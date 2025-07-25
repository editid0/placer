from flask import Flask, render_template, send_from_directory, request, redirect
from flask_socketio import SocketIO
from flask_socketio import send, emit, join_room
from dotenv import load_dotenv
import os, psycopg2, uuid, math, re
from limits import storage, strategies, parse
import valkey
import cachetools.func

load_dotenv()  # Load environment variables from .env file

# Get environment variables
SECRET_KEY = os.getenv("SECRET_KEY")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")
# Done getting environment variables

# Initialise the flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

# Initialise socketio
socketio = SocketIO(app)

# Initialise the postgres connection
database = psycopg2.connect(
    host=DB_HOST, user=DB_USER, password=DB_PASS, dbname=DB_NAME
)

# Initialise the rate limiter
backend = storage.MemoryStorage()
strategy = strategies.SlidingWindowCounterRateLimiter(backend)
ratelimit_limit = parse("10 per second")
higher_limit = parse("50 per second")

# Initialise Valkey connection
kv = valkey.Valkey(host=os.getenv("DB_HOST"), port=6379, db=0)
names_kv = valkey.Valkey(host=os.getenv("DB_HOST"), port=6379, db=1)


@cachetools.func.ttl_cache(maxsize=100, ttl=300)
def calculate_leaderboard():
    """
    Calculate the leaderboard based on pixel counts.
    We cache results for 5 minutes to reduce load on the database, as the entire database is scanned.
    """
    leaderboard = []
    all_pixels = kv.keys()
    for user_id in all_pixels:
        pixels = kv.get(user_id)
        if pixels is not None:
            pixels = int(pixels)
            name = names_kv.get(user_id)
            name = name.decode("utf-8") if name else "Unknown"
            leaderboard.append({"user_id": user_id, "name": name, "pixels": pixels})
    sorter = lambda x: x["pixels"]
    leaderboard.sort(key=sorter, reverse=True)
    return leaderboard


def pixels_to_level(pixels):
    """
    Convert pixel count to level.
    """
    if pixels < 0:
        return 0
    return math.floor(math.sqrt(pixels) / 5)


@app.route("/")
def index():
    # The home page for the app, all frontend functionality is handled in this file
    return render_template("index.html", room_name="main")


@app.route("/room/<room_name>")
def room(room_name):
    room_name = re.sub(r"[^a-zA-Z0-9]", "", room_name.lower())  # Sanitize room name
    if not room_name:
        # If no room name is provided, redirect to the main room
        return redirect("/room/main")
    if room_name in ["main"]:
        return redirect("/")
    return render_template("index.html", room_name=room_name)


@app.route("/leaderboard")
def leaderboard():
    # The leaderboard page, this shows the top users based on pixel count
    leaderboard = calculate_leaderboard()
    cache_stats = calculate_leaderboard.cache_info()
    return render_template(
        "leaderboard.html", leaderboard=leaderboard, cache_stats=cache_stats
    )


@app.route("/public/<path:filename>")
def public(filename):
    # Serve static files from the public directory, mainly CSS
    return send_from_directory("public", filename)


@socketio.on("request_current")
def handle_message(data={}):
    # Handle the request for current pixels
    with database.cursor() as cursor:
        # Fetch all pixels from the database that aren't white to save bandwidth
        cursor.execute(
            "SELECT x, y, color FROM pixels WHERE color != '#ffffff' AND room_name = %s",
            (data.get("room_name", "main"),),
        )
        pixels = cursor.fetchall()
    # If the user has an ID, then that is used, otherwise we generate a new one
    # The client will update the user ID if it doesn't have one
    if not data.get("user_id", None):
        user_id = str(uuid.uuid4())
    else:
        user_id = data.get("user_id")
    # Send the user ID back to the client.
    emit("current_pixels", {"pixels": pixels, "user_id": user_id})


@socketio.on("set_coordinate")
def handle_set_coordinate(data):
    # This function handles when a client sets a pixel.
    if not data:
        # If no data is provided, then we send an error message,
        # as either the user is trying to send their own data,
        # or something went wrong
        emit("error_msg", {"message": "No data provided"})
        return
    if not data.get("user_id", None):
        # If no user ID is provided, then we send an error message,
        # as this likely means that the user removed local storage,
        # and the client will get a new user ID when it refreshes
        emit("error_msg", {"message": "User ID is required"})
        return
    if not data.get("room_name", None):
        # If no room name is provided, then we send an error message,
        # as this likely means that the user removed local storage,
        # and the client will get a new room name when it refreshes
        emit("error_msg", {"message": "Room name is required"})
        return
    room_name = data.get("room_name", "main")
    print(
        f"User {data.get('user_id')} is setting coordinate ({data.get('x')}, {data.get('y')}) in room {room_name}"
    )
    pixels = kv.get(
        data.get("user_id")
    )  # Get the pixel count for the user from the Valkey database
    if pixels is None:
        # User does not have any pixels set, so we initialise it as 0
        pixels = 0
    else:
        # The user does have pixels set, so we use their current pixel count
        pixels = int(pixels)
    if pixels_to_level(pixels + 1) >= 15:
        # If the user has reached level 15, we apply a higher rate limit
        limiter = higher_limit
    else:
        # if not, we apply the normal rate limit
        limiter = ratelimit_limit
    # Hit the rate limit strategy, this returns True if the user is allowed, and False otherwise.
    valid = strategy.hit(limiter, "set_coordinate", data.get("user_id"))
    if not valid:
        # If the user is not allowed to send a coordinate, we cancel the request
        return
    # Increment the pixel count for the user
    kv.set(data.get("user_id"), pixels + 1)
    # Send the pixel count back to the client, this allows the client to calculate levels
    emit("pixel_count", {"count": pixels + 1, "user_id": data.get("user_id")})
    # Send the ratelimit remaining to the client, allowing the client to display the current rate limit status
    emit(
        "ratelimit",
        {
            "remaining": strategy.get_window_stats(
                limiter, "set_coordinate", data.get("user_id")
            ).remaining
        },
    )
    x = data.get("x")
    # Make sure x is an integer
    if not isinstance(x, int):
        # If x is not an integer, we send an error message
        emit("error_msg", {"message": "x must be an integer"})
        return
    # Make sure x is between 0 and 99
    if x < 0 or x >= 100:
        # If x is not in the range, we send an error message
        emit("error_msg", {"message": "x coordinate out of bounds"})
        return
    y = data.get("y")
    # Make sure y is an integer
    if not isinstance(y, int):
        # If y is not an integer, we send an error message
        emit("error_msg", {"message": "y must be an integer"})
        return
    # Make sure y is between 0 and 499
    if y < 0 or y >= 100:
        # If y is not in the range, we send an error message
        emit("error_msg", {"message": "y coordinate out of bounds"})
        return
    # Get the colour from the data that the client sent, if no color is provided, we default to white
    color = data.get("color", "#ffffff")
    # Make sure color is a valid (allowed) hex color
    APPROVED = ["#ffffff", "#ffff00", "#0000ff", "#00ff00", "#ff0000"]
    allowed = False
    if pixels_to_level(pixels + 1) >= 10:
        if re.match(r"^#[0-9a-fA-F]{6}$", color):
            # If the color is a valid hex color, we allow it
            allowed = True
        else:
            # If the color is not a valid hex color, we send an error message
            emit("error_msg", {"message": "Invalid color format"})
            return
    else:
        if color in APPROVED:
            allowed = True
    if not allowed:
        # If the color is not allowed, we send an error message
        emit("error_msg", {"message": "Color not allowed"})
        return
    with database.cursor() as cursor:
        # SQL query to insert or update the pixel in the database
        upsert_query = """
        INSERT INTO pixels (x, y, color, room_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (x, y, room_name) DO UPDATE SET color = EXCLUDED.color, updated_at = NOW();
        """
        cursor.execute(upsert_query, (x, y, color, room_name))
        # Commit the changes to the database
        database.commit()
    # Send the message to all connected clients, so they can update their UI, keeping the UI in sync
    emit(
        "coordinate_set",
        {"x": x, "y": y, "color": color},
        broadcast=True,
        room=room_name,
    )


@socketio.on("ratelimit")
def handle_ratelimit(data):
    # This function is called when the client requests the current rate limit status
    if not data:
        # If no data is provided, we send an error message
        emit("error_msg", {"message": "No data provided"})
        return
    user_id = data.get("user_id")
    if not user_id:
        # If no user ID is provided, we send an error message
        emit("error_msg", {"message": "User ID is required"})
        return
    pixels = kv.get(data.get("user_id"))
    if pixels is None:
        # If the user does not have any pixels set, we set the pixel count to 0
        pixels = 0
    else:
        # If the user has pixels set, we convert the pixel count to an integer
        pixels = int(pixels)
    if pixels_to_level(pixels) >= 15:
        # If the user has reached level 15, we apply a higher rate limit
        limiter = higher_limit
    else:
        # if not, we apply the normal rate limit
        limiter = ratelimit_limit
    remaining = strategy.get_window_stats(limiter, "set_coordinate", user_id).remaining
    # Send the remaining requests to the client
    emit("ratelimit", {"remaining": remaining, "user_id": user_id})


@socketio.on("pixel_count")
def handle_pixel_count(data):
    # This function is called when the client requests the pixel count
    if not data.get("user_id", None):
        # If no user ID is provided, we send an error message
        emit("error_msg", {"message": "User ID is required"})
        return
    # Get the pixel count for the user from the Valkey database
    pixels = kv.get(data.get("user_id"))
    if pixels is None:
        # If the user does not have any pixels set, we set the pixel count to 0
        pixels = 0
    else:
        # If the user has pixels set, we convert the pixel count to an integer
        pixels = int(pixels)
    # Send the pixel count back to the client
    emit("pixel_count", {"count": pixels, "user_id": data.get("user_id")})


@socketio.on("get_name")
def handle_get_name(data):
    # This function is called when the client requests the name of the user
    if not data.get("user_id", None):
        # If no user ID is provided, we send an error message
        emit("error_msg", {"message": "User ID is required"})
        return
    # Get the name for the user from the Valkey database
    name = (
        names_kv.get(data.get("user_id")).decode("utf-8")
        if names_kv.get(data.get("user_id"))
        else None
    )
    if name is None:
        # If the user does not have a name set, we set the name to an empty string
        name = ""
    # Send the name back to the client
    emit("name", {"name": name, "user_id": data.get("user_id")})


@socketio.on("set_name")
def handle_set_name(data):
    print("Setting name for user:", data.get("user_id"))
    # This function is called when the client sets the name of the user
    if not data.get("user_id", None):
        # If no user ID is provided, we send an error message
        emit("error_msg", {"message": "User ID is required"})
        return
    name = data.get("name")
    # Replace any characters not matching this regex with an empty string: [a-zA-Z0-9]{3,10}
    name = re.sub(r"[^a-zA-Z0-9]", "", name)
    # Make sure the name is between 3 and 10 characters long
    if len(name) < 3 or len(name) > 10:
        # If the name is not in the range, we send an error message
        emit("error_msg", {"message": "Name must be between 3 and 10 characters"})
        return
    # Check if the name is taken
    for key in names_kv.keys():
        if names_kv.get(key) == name.encode("utf-8"):
            # If the name is already taken, we send an error message
            emit("error_msg", {"message": "Name already taken"})
            return
    if not name:
        # If no name is provided, we send an error message
        emit("error_msg", {"message": "Name cannot be empty"})
        return
    # Set the name for the user in the Valkey database
    names_kv.set(data.get("user_id"), name)
    # Send the name back to the client
    emit("name", {"name": name, "user_id": data.get("user_id")})


@socketio.on("join_room")
def handle_join_room(data):
    # This function is called when the client joins a room
    room_name = data.get("room_name", "main")
    if not room_name:
        # If no room name is provided, we send an error message
        emit("error_msg", {"message": "Room name is required"})
        return
    # Join the room
    join_room(room_name)
    # Notify the client that they have joined the room
    emit("joined_room", {"room_name": room_name}, room=room_name)


if __name__ == "__main__":
    # This runs the flask app with socket.io, on all interfaces using port 6969.
    socketio.run(
        app, host="0.0.0.0", port=6969, debug=False, allow_unsafe_werkzeug=True
    )

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
        cursor.execute(
            "SELECT x, y, color FROM pixels WHERE color != '#ffffff' AND room_name = %s",
            (data.get("room_name", "main"),),
        )
        pixels = cursor.fetchall()
    if not data.get("user_id", None):
        user_id = str(uuid.uuid4())
    else:
        user_id = data.get("user_id")
    emit("current_pixels", {"pixels": pixels, "user_id": user_id})


@socketio.on("set_coordinate")
def handle_set_coordinate(data):
    # This function handles when a client sets a pixel.
    if not data:
        emit("error_msg", {"message": "No data provided"})
        return
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    if not data.get("room_name", None):
        emit("error_msg", {"message": "Room name is required"})
        return
    room_name = data.get("room_name", "main")
    print(
        f"User {data.get('user_id')} is setting coordinate ({data.get('x')}, {data.get('y')}) in room {room_name}"
    )
    pixels = kv.get(
        data.get("user_id")
    )
    if pixels is None:
        pixels = 0
    else:
        pixels = int(pixels)
    if pixels_to_level(pixels + 1) >= 15:
        limiter = higher_limit
    else:
        limiter = ratelimit_limit
    valid = strategy.hit(limiter, "set_coordinate", data.get("user_id"))
    if not valid:
        return
    kv.set(data.get("user_id"), pixels + 1)
    emit("pixel_count", {"count": pixels + 1, "user_id": data.get("user_id")})
    emit(
        "ratelimit",
        {
            "remaining": strategy.get_window_stats(
                limiter, "set_coordinate", data.get("user_id")
            ).remaining
        },
    )
    x = data.get("x")
    if not isinstance(x, int):
        emit("error_msg", {"message": "x must be an integer"})
        return
    if x < 0 or x >= 100:
        emit("error_msg", {"message": "x coordinate out of bounds"})
        return
    y = data.get("y")
    if not isinstance(y, int):
        emit("error_msg", {"message": "y must be an integer"})
        return
    if y < 0 or y >= 100:
        emit("error_msg", {"message": "y coordinate out of bounds"})
        return
    color = data.get("color", "#ffffff")
    APPROVED = ["#ffffff", "#ffff00", "#0000ff", "#00ff00", "#ff0000"]
    allowed = False
    if pixels_to_level(pixels + 1) >= 10:
        if re.match(r"^#[0-9a-fA-F]{6}$", color):
            allowed = True
        else:
            emit("error_msg", {"message": "Invalid color format"})
            return
    else:
        if color in APPROVED:
            allowed = True
    if not allowed:
        emit("error_msg", {"message": "Color not allowed"})
        return
    with database.cursor() as cursor:
        upsert_query = """
        INSERT INTO pixels (x, y, color, room_name)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (x, y, room_name) DO UPDATE SET color = EXCLUDED.color, updated_at = NOW();
        """
        cursor.execute(upsert_query, (x, y, color, room_name))
        database.commit()
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
        emit("error_msg", {"message": "No data provided"})
        return
    user_id = data.get("user_id")
    if not user_id:
        emit("error_msg", {"message": "User ID is required"})
        return
    pixels = kv.get(data.get("user_id"))
    if pixels is None:
        pixels = 0
    else:
        pixels = int(pixels)
    if pixels_to_level(pixels) >= 15:
        limiter = higher_limit
    else:
        limiter = ratelimit_limit
    remaining = strategy.get_window_stats(limiter, "set_coordinate", user_id).remaining
    emit("ratelimit", {"remaining": remaining, "user_id": user_id})


@socketio.on("pixel_count")
def handle_pixel_count(data):
    # This function is called when the client requests the pixel count
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    pixels = kv.get(data.get("user_id"))
    if pixels is None:
        pixels = 0
    else:
        pixels = int(pixels)
    emit("pixel_count", {"count": pixels, "user_id": data.get("user_id")})


@socketio.on("get_name")
def handle_get_name(data):
    # This function is called when the client requests the name of the user
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    name = (
        names_kv.get(data.get("user_id")).decode("utf-8")
        if names_kv.get(data.get("user_id"))
        else None
    )
    if name is None:
        name = ""
    emit("name", {"name": name, "user_id": data.get("user_id")})


@socketio.on("set_name")
def handle_set_name(data):
    # This function is called when the client sets the name of the user
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    name = data.get("name")
    name = re.sub(r"[^a-zA-Z0-9]", "", name)
    if len(name) < 3 or len(name) > 10:
        emit("error_msg", {"message": "Name must be between 3 and 10 characters"})
        return
    for key in names_kv.keys():
        if names_kv.get(key) == name.encode("utf-8"):
            emit("error_msg", {"message": "Name already taken"})
            return
    if not name:
        emit("error_msg", {"message": "Name cannot be empty"})
        return
    names_kv.set(data.get("user_id"), name)
    emit("name", {"name": name, "user_id": data.get("user_id")})


@socketio.on("join_room")
def handle_join_room(data):
    # This function is called when the client joins a room
    room_name = data.get("room_name", "main")
    if not room_name:
        emit("error_msg", {"message": "Room name is required"})
        return
    join_room(room_name)
    emit("joined_room", {"room_name": room_name}, room=room_name)


if __name__ == "__main__":
    # This runs the flask app with socket.io, on all interfaces using port 6969.
    socketio.run(
        app, host="0.0.0.0", port=6969, debug=False, allow_unsafe_werkzeug=True
    )

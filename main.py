from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO
from flask_socketio import send, emit
from dotenv import load_dotenv
import os, psycopg2, uuid
from limits import storage, strategies, parse
import valkey

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")


app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app)
database = psycopg2.connect(
    host=DB_HOST, user=DB_USER, password=DB_PASS, dbname=DB_NAME
)
backend = storage.MemoryStorage()
strategy = strategies.SlidingWindowCounterRateLimiter(backend)
ratelimit_limit = parse("10 per second")
kv = valkey.Valkey(host=os.getenv("DB_HOST"), port=6379, db=0)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/public/<path:filename>")
def public(filename):
    return send_from_directory("public", filename)


@socketio.on("request_current")
def handle_message(data={}):
    print("Received request_current")
    with database.cursor() as cursor:
        cursor.execute("SELECT x, y, color FROM pixels")
        pixels = cursor.fetchall()
    if not data.get("user_id", None):
        user_id = str(uuid.uuid4())
    else:
        user_id = data.get("user_id")
    emit("current_pixels", {"pixels": pixels, "user_id": user_id})


@socketio.on("set_coordinate")
def handle_set_coordinate(data):
    if not data:
        emit("error_msg", {"message": "No data provided"})
        return
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    valid = strategy.hit(ratelimit_limit, "set_coordinate", data.get("user_id"))
    if not valid:
        return
    print("Received set_coordinate:", data)
    pixels = kv.get(data.get("user_id"))
    # This shows how many pixels the user has set
    if pixels is None:
        pixels = 0
    else:
        pixels = int(pixels)
    # Increment the pixel count for the user
    kv.set(data.get("user_id"), pixels + 1)
    emit("pixel_count", {"count": pixels + 1, "user_id": data.get("user_id")})
    emit(
        "ratelimit",
        {
            "remaining": strategy.get_window_stats(
                ratelimit_limit, "set_coordinate", data.get("user_id")
            ).remaining
        },
    )
    x = data.get("x")
    # Make sure x is an integer
    if not isinstance(x, int):
        emit("error_msg", {"message": "x must be an integer"})
        return
    # Make sure x is between 0 and 99
    if x < 0 or x >= 100:
        emit("error_msg", {"message": "x coordinate out of bounds"})
        return
    y = data.get("y")
    # Make sure y is an integer
    if not isinstance(y, int):
        emit("error_msg", {"message": "y must be an integer"})
        return
    # Make sure y is between 0 and 499
    if y < 0 or y >= 100:
        emit("error_msg", {"message": "y coordinate out of bounds"})
        return
    color = data.get("color", "#ffffff")
    # Make sure color is a valid hex color
    APPROVED = ["#ffffff", "#ffff00", "#0000ff", "#00ff00", "#ff0000"]
    with database.cursor() as cursor:
        upsert_query = """
        INSERT INTO pixels (x, y, color)
        VALUES (%s, %s, %s)
        ON CONFLICT (x, y) DO UPDATE SET color = EXCLUDED.color, updated_at = NOW();
        """
        cursor.execute(upsert_query, (x, y, color))
        database.commit()

    emit("coordinate_set", {"x": x, "y": y, "color": color}, broadcast=True)


@socketio.on("ratelimit")
def handle_ratelimit(data):
    user_id = data.get("user_id")
    if not user_id:
        emit("error_msg", {"message": "User ID is required"})
        return
    remaining = strategy.get_window_stats(
        ratelimit_limit, "set_coordinate", user_id
    ).remaining
    emit("ratelimit", {"remaining": remaining, "user_id": user_id})


@socketio.on("pixel_count")
def handle_pixel_count(data):
    if not data.get("user_id", None):
        emit("error_msg", {"message": "User ID is required"})
        return
    pixels = kv.get(data.get("user_id"))
    if pixels is None:
        pixels = 0
    else:
        pixels = int(pixels)
    emit("pixel_count", {"count": pixels, "user_id": data.get("user_id")})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=6969, debug=True)

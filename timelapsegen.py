from dotenv import load_dotenv
import os, psycopg2, io, cv2
from datetime import datetime
from PIL import Image, ImageDraw
import numpy as np

load_dotenv()
DB_HOST = os.getenv("DB_HOST")
DB_PORT = 5432
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

# Grid size
WIDTH = 100
HEIGHT = 100

# Output resolution
OUTPUT_WIDTH = 500
OUTPUT_HEIGHT = 500

# Calculate scaling factors
SCALE_X = OUTPUT_WIDTH // WIDTH
SCALE_Y = OUTPUT_HEIGHT // HEIGHT

# Video generation settings
VIDEO_SECONDS = 60
VIDEO_NAME = "timelapse.mp4"

# Create high-resolution image
image = Image.new("RGB", (OUTPUT_WIDTH, OUTPUT_HEIGHT), "#ffffff")
image_list = []


# Get all data from the last 24 hours from pixel_history
def get_recent_pixels():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )
    cur = conn.cursor()
    cur.execute(
        """
        SELECT x, y, color FROM pixel_history
        WHERE changed_at >= NOW() - INTERVAL '24 hours'
        ORDER BY changed_at DESC;
        """
    )
    pixels = cur.fetchall()
    print(f"Retrieved {len(pixels)} pixels from the last 24 hours")
    cur.close()
    conn.close()
    return pixels


def generate_timelapse():
    pixels = get_recent_pixels()
    draw = ImageDraw.Draw(image)
    print(f"Drawing {len(pixels)} pixels on the image")
    for x, y, color in pixels:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            # Scale up the coordinates for high-resolution output
            x1 = x * SCALE_X
            y1 = y * SCALE_Y
            x2 = (x + 1) * SCALE_X
            y2 = (y + 1) * SCALE_Y
            # Draw each pixel as a scaled rectangle in the image
            draw.rectangle([x1, y1, x2, y2], fill=color)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Save the image to a BytesIO object
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format="JPEG", quality=90)
            img_byte_arr.seek(0)
            image_list.append((img_byte_arr, timestamp))
    return image_list


if __name__ == "__main__":
    generate_timelapse()
    print(len(image_list), "images generated for timelapse")
    # Combine images into an mp4 video
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    video_writer = cv2.VideoWriter(
        VIDEO_NAME,
        fourcc,
        (len(image_list) // VIDEO_SECONDS),
        (OUTPUT_WIDTH, OUTPUT_HEIGHT),
    )
    for img, timestamp in image_list:
        img_cv = cv2.imdecode(np.frombuffer(img.getvalue(), np.uint8), cv2.IMREAD_COLOR)
        video_writer.write(img_cv)
    video_writer.release()
    print(f"Timelapse video saved as {VIDEO_NAME}")

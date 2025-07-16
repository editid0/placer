import psycopg2, os
from dotenv import load_dotenv

load_dotenv()

# Database connection details - replace with your own
DB_HOST = os.getenv("DB_HOST")
DB_PORT = 5432
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

# Grid size
WIDTH = 100
HEIGHT = 100


def fill_grid_with_color(color="#ffffff"):
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )
    cur = conn.cursor()

    # Upsert query: insert or update color for each pixel
    upsert_query = """
    INSERT INTO pixels (x, y, color)
    VALUES (%s, %s, %s)
    ON CONFLICT (x, y) DO UPDATE SET color = EXCLUDED.color, updated_at = NOW();
    """

    for x in range(WIDTH):
        for y in range(HEIGHT):
            cur.execute(upsert_query, (x, y, color))

    conn.commit()
    cur.close()
    conn.close()
    print(f"Filled {WIDTH}x{HEIGHT} grid with color {color}")


if __name__ == "__main__":
    fill_grid_with_color()

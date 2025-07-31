# placer
A multiplayer game where players can draw on a canvas, inspired by r/place

## AI Usage
I used AI for explaining why stuff doesn't work, so for debugging, I didn't use AI to generate large parts of the code, I would estimate about 2-3% AI usage in the code overall.
AI was also used to generated the messages in the commits.

# .env File
```env
SECRET_KEY=YourPassword
DB_HOST=...
DB_NAME=...
DB_USER=...
DB_PASS=...
```

# Database tables
## Postgres
```sql
CREATE TABLE pixels (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    color CHAR(7) NOT NULL CHECK (color ~ '^#([A-Fa-f0-9]{6})$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (x, y)
);
```
```sql
CREATE TABLE pixel_history (
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    color CHAR(7) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
```
CREATE OR REPLACE FUNCTION log_pixel_update() 
RETURNS TRIGGER AS $$
BEGIN
    -- On UPDATE or DELETE, insert old pixel data into pixel_history
    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') THEN
        INSERT INTO pixel_history (x, y, color, changed_at)
        VALUES (OLD.x, OLD.y, OLD.color, NOW());
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```
```
CREATE TRIGGER trg_log_pixel_update
AFTER UPDATE OR DELETE ON pixels
FOR EACH ROW EXECUTE FUNCTION log_pixel_update();
```
## Valkey
Valkey doesn't have tables, but it connects to the same host as the postgres, so make sure you're running them on the same machine.

# Before running
First, it's recommended to run the populate.py file, as this will fill all cells with the correct colour in the database.
```
python3 main.py
```

# Dependencies
There is a requirements.txt file at the root of the project, run the following command to install dependencies
```
pip3 install -r requirements.txt
```
# Running
To run the main server, run the following command:
```
python3 main.py
```

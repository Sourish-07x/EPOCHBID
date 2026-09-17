import asyncio
import os
import asyncpg
import ssl
from dotenv import load_dotenv

load_dotenv()

async def check():
    try:
        # Create a permissive SSL context to bypass network interception
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        # Strip the URL parameter to prevent conflicts with our explicit SSL context
        db_url = os.getenv("DATABASE_URL").replace("?sslmode=require", "")

        # Connect to Neon
        conn = await asyncpg.connect(db_url, ssl=ssl_ctx)
        version = await conn.fetchval("SELECT version();")
        
        print("\n--- SUCCESS! Connected to Neon ---")
        print(f"Database Info: {version}\n")
        await conn.close()
    except Exception as e:
        print(f"\nConnection failed: {e}\n")

asyncio.run(check())
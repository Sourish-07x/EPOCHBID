import os
import ssl
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import asyncpg
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from collections import deque
import time

load_dotenv()

# Global states
SWARM_CONFIG = {"starting_bid": 10000}
RECENT_LOGS = deque(maxlen=40)
AUCTION_STATE = {
    "status": "ACTIVE",      # ACTIVE, CLOSED
    "timer_mode": "AUTO",    # AUTO, MANUAL
    "last_bid_time": time.time(),
    "countdown": 10
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    db_url = os.getenv("DATABASE_URL").replace("?sslmode=require", "")
    
    app.state.pool = await asyncpg.create_pool(db_url, ssl=ssl_ctx, min_size=10, max_size=100)
    
    async with app.state.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS final_auction (
                id SERIAL PRIMARY KEY,
                item_name TEXT NOT NULL,
                min_bid INT NOT NULL,
                highest_bid INT DEFAULT 0,
                winner_name TEXT DEFAULT 'System'
            );
        """)
        await conn.execute("""
            INSERT INTO final_auction (id, item_name, min_bid, highest_bid, winner_name) 
            VALUES (1, 'Epoch GPU Cluster', 5000, 5000, 'System')
            ON CONFLICT (id) DO NOTHING;
        """)
    yield 
    await app.state.pool.close()

app = FastAPI(lifespan=lifespan)

class BidRequest(BaseModel):
    user_name: str
    amount: int

@app.post("/bid")
async def place_bid(bid: BidRequest):
    global AUCTION_STATE
    if AUCTION_STATE["status"] == "CLOSED":
        raise HTTPException(status_code=400, detail="Auction is closed.")

    async with app.state.pool.acquire() as conn:
        try:
            async with conn.transaction(isolation='serializable'):
                record = await conn.fetchrow("SELECT min_bid, highest_bid FROM final_auction WHERE id = 1")
                
                if bid.amount < record['min_bid']:
                    raise HTTPException(status_code=400, detail=f"Rejected. Min bid is ₹{record['min_bid']}")
                if bid.amount <= record['highest_bid']:
                    raise HTTPException(status_code=400, detail=f"Rejected. Highest is ₹{record['highest_bid']}")
                
                await conn.execute(
                    "UPDATE final_auction SET highest_bid = $1, winner_name = $2 WHERE id = 1", 
                    bid.amount, bid.user_name
                )
            
            # Reset timer on valid bid
            AUCTION_STATE["last_bid_time"] = time.time()
            AUCTION_STATE["countdown"] = 10

            log_msg = f"₹{bid.amount:,} | {bid.user_name} (Lock Secured)"
            RECENT_LOGS.appendleft({"type": "success", "msg": log_msg})
            print(f"\033[92m[VALID] {log_msg}\033[0m")
            return {"status": "Transaction Approved!"}
            
        except asyncpg.exceptions.SerializationError:
            log_msg = f"₹{bid.amount:,} | {bid.user_name} (Race Mitigated)"
            RECENT_LOGS.appendleft({"type": "conflict", "msg": log_msg})
            print(f"\033[91m[BLOCKED] {log_msg}\033[0m")
            raise HTTPException(status_code=409, detail="Race condition mitigated.")

@app.post("/api/reset")
async def reset_auction():
    global SWARM_CONFIG, RECENT_LOGS, AUCTION_STATE
    SWARM_CONFIG["starting_bid"] = 5000
    RECENT_LOGS.clear()
    AUCTION_STATE["status"] = "ACTIVE"
    AUCTION_STATE["last_bid_time"] = time.time()
    AUCTION_STATE["countdown"] = 10
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE final_auction SET highest_bid = 5000, winner_name = 'System' WHERE id = 1")
    return {"status": "Database Reset"}

@app.post("/api/auction/close")
async def close_auction():
    global AUCTION_STATE
    AUCTION_STATE["status"] = "CLOSED"
    return {"status": "Auction Closed"}

@app.post("/api/auction/toggle-mode")
async def toggle_mode():
    global AUCTION_STATE
    if AUCTION_STATE["timer_mode"] == "AUTO":
        AUCTION_STATE["timer_mode"] = "MANUAL"
    else:
        AUCTION_STATE["timer_mode"] = "AUTO"
        AUCTION_STATE["last_bid_time"] = time.time()
        AUCTION_STATE["countdown"] = 10
    return {"mode": AUCTION_STATE["timer_mode"]}

@app.get("/api/locust/config")
async def get_locust_config():
    return SWARM_CONFIG

@app.post("/api/locust/start")
async def start_locust(users: int = 50, spawn_rate: int = 10, starting_bid: int = 10000):
    global SWARM_CONFIG
    SWARM_CONFIG["starting_bid"] = starting_bid
    async with httpx.AsyncClient() as client:
        try:
            await client.post("http://localhost:8089/swarm", data={"user_count": users, "spawn_rate": spawn_rate, "host": "http://127.0.0.1:8000"}, timeout=2.0)
            return {"status": "started"}
        except Exception:
            raise HTTPException(status_code=500, detail="Locust offline")

@app.post("/api/locust/stop")
async def stop_locust():
    async with httpx.AsyncClient() as client:
        try:
            await client.get("http://localhost:8089/stop", timeout=2.0)
            return {"status": "stopped"}
        except Exception:
            raise HTTPException(status_code=500, detail="Locust offline")

@app.get("/api/data")
async def get_system_data():
    global AUCTION_STATE
    
    # Handle Auto Countdown Logic
    if AUCTION_STATE["status"] == "ACTIVE" and AUCTION_STATE["timer_mode"] == "AUTO":
        elapsed = time.time() - AUCTION_STATE["last_bid_time"]
        remaining = max(0, 10 - int(elapsed))
        AUCTION_STATE["countdown"] = remaining
        if remaining == 0:
            AUCTION_STATE["status"] = "CLOSED"

    data = {
        "db_online": False, 
        "locust_state": "offline", 
        "rps": 0, 
        "failures": 0, 
        "market": {}, 
        "logs": list(RECENT_LOGS),
        "auction": AUCTION_STATE
    }
    
    try:
        async with app.state.pool.acquire() as conn:
            record = await conn.fetchrow("SELECT * FROM final_auction WHERE id = 1")
            data["market"] = dict(record) if record else {}
            data["db_online"] = True
    except Exception:
        pass
        
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get("http://localhost:8089/stats/requests", timeout=1.0)
            if res.status_code == 200:
                locust = res.json()
                data["locust_state"] = locust.get("state", "offline")
                data["rps"] = round(locust.get("total_rps", 0), 1)
                data["failures"] = round(locust.get("fail_ratio", 0) * 100, 1)
        except Exception:
            pass
            
    return data

# --- FRONTEND UI ---
@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>EpochBid | Master Terminal</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800;900&family=JetBrains+Mono:wght@400;700;800&display=swap');
            body { font-family: 'Inter', sans-serif; background-color: #030712; color: #f8fafc; background-image: linear-gradient(to bottom, rgba(3, 7, 18, 0.88), rgba(3, 7, 18, 0.95)), url('https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?q=80&w=2000&auto=format&fit=crop'); background-size: cover; background-position: center; background-attachment: fixed; }
            .font-mono-num { font-family: 'JetBrains Mono', monospace; }
            .glass-panel { background: rgba(10, 15, 30, 0.75); backdrop-filter: blur(24px); border: 1px solid rgba(34, 211, 238, 0.15); box-shadow: inset 0 0 20px rgba(34, 211, 238, 0.02), 0 20px 40px rgba(0,0,0,0.6); }
            .glow-text { text-shadow: 0 0 30px rgba(34, 211, 238, 0.6), 0 0 10px rgba(255,255,255,0.8); }
            .glow-red { text-shadow: 0 0 20px rgba(244, 63, 94, 0.6); }
            ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 10px; }
        </style>
    </head>
    <body class="min-h-screen flex flex-col">
        
        <!-- Top Navigation Bar -->
        <nav class="border-b border-slate-800/80 bg-slate-950/80 sticky top-0 z-50 backdrop-blur-xl">
            <div class="max-w-7xl mx-auto px-6 py-4 flex justify-between items-center">
                <div class="flex items-center gap-4">
                    <h1 class="text-2xl font-black tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-blue-500">EpochBid.</h1>
                    <div class="h-6 w-px bg-slate-700 mx-2"></div>
                    <!-- Countdown / Timer Status Pill -->
                    <div onclick="toggleTimerMode()" id="timerPill" class="cursor-pointer bg-cyan-500/10 border border-cyan-500/30 text-cyan-400 px-3 py-1 rounded-full text-xs font-black font-mono-num tracking-wider flex items-center gap-2 hover:bg-cyan-500/20 transition">
                        <div class="w-2 h-2 rounded-full bg-cyan-400 animate-ping"></div>
                        <span id="timerText">AUTO COUNTDOWN: 10s</span>
                    </div>
                </div>
                <div class="flex gap-3 items-center">
                    <button onclick="resetSystem()" class="bg-rose-500/10 text-rose-500 border border-rose-500/30 hover:bg-rose-500 hover:text-white px-4 py-1.5 rounded-lg text-xs font-black uppercase tracking-widest transition-all">Reset Engine</button>
                    <div class="flex items-center gap-2 bg-slate-900/80 px-4 py-2 rounded-lg border border-slate-700/50">
                        <div id="dbIndicator" class="w-2 h-2 rounded-full bg-rose-500"></div>
                        <span id="dbStatusText" class="text-[10px] font-black text-slate-400 uppercase tracking-widest">DB</span>
                    </div>
                    <div class="flex items-center gap-2 bg-slate-900/80 px-4 py-2 rounded-lg border border-slate-700/50">
                        <div id="locustIndicator" class="w-2 h-2 rounded-full bg-rose-500"></div>
                        <span id="locustStatusText" class="text-[10px] font-black text-slate-400 uppercase tracking-widest">Tester</span>
                    </div>
                </div>
            </div>
        </nav>

        <!-- WINNER MODAL POPUP -->
        <div id="winnerModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-md z-50 hidden flex items-center justify-center p-4 animate-fade-in">
            <div class="glass-panel p-8 rounded-3xl max-w-md w-full border-2 border-cyan-400 shadow-[0_0_50px_rgba(34,211,238,0.3)] text-center">
                <div class="text-xs text-cyan-400 font-black uppercase tracking-widest mb-2">🎉 Auction Concluded</div>
                <h2 class="text-3xl font-black text-white mb-6">Winning Bidder Verified</h2>
                
                <div class="bg-slate-900/80 border border-slate-700 p-6 rounded-2xl mb-6 space-y-3">
                    <div>
                        <div class="text-[10px] text-slate-500 uppercase font-bold">Winning Entity</div>
                        <div id="modalWinner" class="text-xl font-black text-cyan-400 font-mono-num">...</div>
                    </div>
                    <div>
                        <div class="text-[10px] text-slate-500 uppercase font-bold">Final Settlement Price</div>
                        <div id="modalPrice" class="text-3xl font-black text-white font-mono-num">₹0</div>
                    </div>
                </div>

                <button onclick="resetSystem()" class="w-full bg-gradient-to-r from-cyan-500 to-blue-500 hover:from-cyan-400 hover:to-blue-400 text-slate-950 font-black tracking-widest uppercase py-3.5 rounded-xl shadow-lg transition-all">Start New Auction</button>
            </div>
        </div>

        <!-- Main Unified Grid Container -->
        <main class="flex-1 max-w-7xl mx-auto w-full px-6 py-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
            
            <!-- LEFT 2 COLUMNS: Market Dashboard & Chart -->
            <div class="lg:col-span-2 glass-panel p-6 rounded-3xl flex flex-col justify-between">
                <div>
                    <div class="flex flex-col sm:flex-row justify-between items-start mb-6">
                        <div>
                            <h2 class="text-xs text-slate-400 uppercase tracking-widest font-bold mb-2">Epoch GPU Cluster Valuation</h2>
                            <div class="text-[3.5rem] sm:text-[4rem] leading-none font-black text-white glow-text font-mono-num tracking-tight" id="priceDisplay">₹0</div>
                            <div class="text-sm text-slate-400 mt-3">Leading Bidder: <span id="winnerDisplay" class="text-cyan-400 font-bold px-3 py-1 bg-cyan-400/10 rounded-md border border-cyan-400/30 shadow-[0_0_10px_rgba(34,211,238,0.2)]">...</span></div>
                        </div>
                        <div class="flex gap-3 mt-4 sm:mt-0">
                            <div class="bg-slate-900/60 border border-slate-700/50 p-3.5 rounded-2xl text-right min-w-[110px] shadow-lg backdrop-blur-md">
                                <div class="text-2xl font-black text-white font-mono-num" id="rpsDisplay">0.0</div>
                                <div class="text-[9px] text-cyan-500 uppercase tracking-widest mt-1 font-bold">Req / Sec</div>
                            </div>
                            <div class="bg-slate-900/60 border border-slate-700/50 p-3.5 rounded-2xl text-right min-w-[110px] shadow-lg backdrop-blur-md">
                                <div class="text-2xl font-black text-emerald-400 font-mono-num" id="failDisplay">0%</div>
                                <div class="text-[9px] text-emerald-500 uppercase tracking-widest mt-1 font-bold">Mitigated</div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="bg-slate-950/80 border border-slate-800 rounded-2xl p-4 relative h-[260px] mb-6 shadow-inner">
                        <canvas id="liveChart"></canvas>
                    </div>
                </div>
                
                <div class="grid grid-cols-2 sm:grid-cols-5 gap-3 bg-slate-900/40 p-4 rounded-2xl border border-slate-700/50 backdrop-blur-md">
                    <div>
                        <label class="block text-[9px] font-bold text-slate-400 uppercase tracking-widest mb-1.5 ml-0.5">Users</label>
                        <input type="number" id="testUsers" value="100" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-3 py-2.5 text-sm text-center outline-none focus:border-cyan-400 transition font-mono-num">
                    </div>
                    <div>
                        <label class="block text-[9px] font-bold text-slate-400 uppercase tracking-widest mb-1.5 ml-0.5">Rate</label>
                        <input type="number" id="testRate" value="20" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-3 py-2.5 text-sm text-center outline-none focus:border-cyan-400 transition font-mono-num">
                    </div>
                    <div>
                        <label class="block text-[9px] font-bold text-cyan-400 uppercase tracking-widest mb-1.5 ml-0.5">Start Bid</label>
                        <input type="number" id="testStartBid" value="10000" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-3 py-2.5 text-sm text-center outline-none focus:border-cyan-400 transition font-bold text-cyan-400 font-mono-num">
                    </div>
                    <div class="col-span-2 sm:col-span-1 flex items-end">
                        <button onclick="startTest()" class="w-full bg-gradient-to-r from-rose-600 to-rose-500 text-white hover:from-rose-500 hover:to-rose-400 transition-all rounded-xl font-black text-[10px] tracking-widest uppercase py-3 shadow-[0_0_15px_rgba(225,29,72,0.4)]">Ignite</button>
                    </div>
                    <div class="col-span-2 sm:col-span-1 flex items-end">
                        <button onclick="stopTest()" class="w-full bg-slate-800 text-slate-300 hover:bg-slate-700 hover:text-white border border-slate-600 transition-all rounded-xl font-bold text-[10px] tracking-widest uppercase py-3">Halt</button>
                    </div>
                </div>
            </div>

            <!-- RIGHT COLUMN: Manual Terminal & Live Transaction Feed -->
            <div class="flex flex-col gap-6">
                
                <div class="glass-panel p-6 rounded-3xl border-t-4 border-t-emerald-400">
                    <h2 class="text-lg font-black mb-1 text-white tracking-tight">Manual Terminal</h2>
                    <p class="text-xs text-slate-400 mb-4 font-medium">Inject a test transaction instantly.</p>
                    <div class="space-y-3">
                        <div>
                            <label class="block text-[10px] font-bold text-emerald-500 uppercase tracking-widest mb-1">Trader Identity</label>
                            <input type="text" id="userName" placeholder="e.g. Sourish_Alpha" class="w-full bg-slate-950/80 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-400 shadow-inner font-mono-num">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-emerald-500 uppercase tracking-widest mb-1">Amount (₹)</label>
                            <input type="number" id="bidAmount" placeholder="0" class="w-full bg-slate-950/80 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-400 shadow-inner font-mono-num">
                        </div>
                        
                        <div>
                            <label class="block text-[9px] font-bold text-slate-400 uppercase tracking-widest mb-1.5">Instant Increment Bids</label>
                            <div class="grid grid-cols-4 gap-1.5">
                                <button onclick="quickBid(100)" class="bg-slate-900 hover:bg-emerald-500 hover:text-slate-950 text-cyan-400 border border-cyan-500/30 text-[10px] font-bold py-2 rounded-lg font-mono-num transition shadow">+₹100</button>
                                <button onclick="quickBid(500)" class="bg-slate-900 hover:bg-emerald-500 hover:text-slate-950 text-cyan-400 border border-cyan-500/30 text-[10px] font-bold py-2 rounded-lg font-mono-num transition shadow">+₹500</button>
                                <button onclick="quickBid(1000)" class="bg-slate-900 hover:bg-emerald-500 hover:text-slate-950 text-cyan-400 border border-cyan-500/30 text-[10px] font-bold py-2 rounded-lg font-mono-num transition shadow">+₹1K</button>
                                <button onclick="quickBid(5000)" class="bg-slate-900 hover:bg-emerald-500 hover:text-slate-950 text-cyan-400 border border-cyan-500/30 text-[10px] font-bold py-2 rounded-lg font-mono-num transition shadow">+₹5K</button>
                            </div>
                        </div>

                        <button onclick="placeBid()" class="w-full mt-2 bg-gradient-to-r from-emerald-500 to-emerald-400 hover:from-emerald-400 hover:to-emerald-300 text-slate-950 font-black tracking-widest uppercase py-3 rounded-xl shadow-[0_0_20px_rgba(16,185,129,0.3)] transition-all text-xs">Submit Transaction</button>
                        <div id="bidStatusMsg" class="text-center text-xs font-bold h-4 mt-2 opacity-0 transition-opacity"></div>
                    </div>
                </div>

                <div class="glass-panel p-6 rounded-3xl flex-1 flex flex-col min-h-[220px]">
                    <h3 class="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                        <div class="w-2 h-2 rounded-full bg-cyan-400 animate-pulse"></div> Transaction Log Stream
                    </h3>
                    <div id="logContainer" class="flex-1 overflow-y-auto space-y-2 pr-1 font-mono-num text-[11px] flex flex-col max-h-[220px]">
                        <!-- Logs injected here -->
                    </div>
                </div>

            </div>

        </main>

        <script>
            let chartInstance = null;
            let latestHighestBid = 0;
            const ctx = document.getElementById('liveChart').getContext('2d');
            let gradient = ctx.createLinearGradient(0, 0, 0, 300);
            gradient.addColorStop(0, 'rgba(34, 211, 238, 0.4)'); gradient.addColorStop(1, 'rgba(34, 211, 238, 0.0)');

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [{ data: [], borderColor: '#22d3ee', backgroundColor: gradient, borderWidth: 3, fill: true, tension: 0.4, pointRadius: 0, pointHoverRadius: 6 }] },
                options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 }, scales: { x: { display: false }, y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b', font: {family: 'JetBrains Mono', size: 10}, callback: function(value) { return '₹' + value.toLocaleString('en-IN'); } } } }, plugins: { legend: { display: false } } }
            });

            async function toggleTimerMode() {
                const res = await fetch('/api/auction/toggle-mode', { method: 'POST' });
                const data = await res.json();
                if(data.mode === 'MANUAL') {
                    document.getElementById('timerText').innerText = "MODE: MANUAL CLOSE";
                }
            }

            async function quickBid(increment) {
                let user_name = document.getElementById('userName').value.trim();
                if(!user_name) { user_name = "Sourish_Manual"; document.getElementById('userName').value = user_name; }
                const amount = (latestHighestBid > 0 ? latestHighestBid : 5000) + increment;
                
                const res = await fetch('/bid', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_name, amount}) });
                const data = await res.json();
                const stat = document.getElementById('bidStatusMsg');
                stat.innerText = res.ok ? `+₹${increment.toLocaleString('en-IN')} Bid Approved!` : data.detail;
                stat.className = `text-center text-xs font-bold h-4 mt-2 opacity-100 ${res.ok ? 'text-emerald-400 glow-text' : 'text-rose-500 glow-red'}`;
                if(res.ok) { document.getElementById('bidAmount').value = ""; setTimeout(() => stat.style.opacity = '0', 3000); }
            }

            async function resetSystem() {
                await fetch('/api/reset', { method: 'POST' });
                document.getElementById('winnerModal').classList.add('hidden');
                chartInstance.data.labels = [];
                chartInstance.data.datasets[0].data = [];
                chartInstance.update();
                document.getElementById('testStartBid').value = 10000;
                latestHighestBid = 5000;
            }

            async function placeBid() {
                const user_name = document.getElementById('userName').value;
                const amount = parseInt(document.getElementById('bidAmount').value);
                const stat = document.getElementById('bidStatusMsg');
                try {
                    const res = await fetch('/bid', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_name, amount}) });
                    const data = await res.json();
                    stat.innerText = res.ok ? "Transaction Approved!" : data.detail;
                    stat.className = `text-center text-xs font-bold h-4 mt-2 opacity-100 ${res.ok ? 'text-emerald-400 glow-text' : 'text-rose-500 glow-red'}`;
                    if(res.ok) { document.getElementById('bidAmount').value = ""; setTimeout(() => stat.style.opacity = '0', 3000); }
                } catch(e) {}
            }

            async function startTest() {
                const u = document.getElementById('testUsers').value;
                const r = document.getElementById('testRate').value;
                const s = document.getElementById('testStartBid').value;
                await fetch(`/api/locust/start?users=${u}&spawn_rate=${r}&starting_bid=${s}`, {method: 'POST'});
            }
            async function stopTest() { await fetch('/api/locust/stop', {method: 'POST'}); }

            async function syncData() {
                try {
                    const res = await fetch('/api/data');
                    const data = await res.json();
                    
                    const dbInd = document.getElementById('dbIndicator'); const dbTxt = document.getElementById('dbStatusText');
                    if(data.db_online) {
                        dbInd.className = "w-2 h-2 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_8px_#34d399]"; dbTxt.className = "text-[10px] font-black text-emerald-400 uppercase tracking-widest";
                        if(data.market && Object.keys(data.market).length !== 0) {
                            latestHighestBid = data.market.highest_bid;
                            document.getElementById('priceDisplay').innerText = '₹' + latestHighestBid.toLocaleString('en-IN');
                            document.getElementById('winnerDisplay').innerText = data.market.winner_name;
                            
                            const time = new Date().toLocaleTimeString();
                            chartInstance.data.labels.push(time);
                            chartInstance.data.datasets[0].data.push(latestHighestBid);
                            if(chartInstance.data.labels.length > 40) { chartInstance.data.labels.shift(); chartInstance.data.datasets[0].data.shift(); }
                            chartInstance.update('none');
                            
                            if (data.locust_state === 'offline' && !document.activeElement.id.includes('testStartBid')) {
                                document.getElementById('testStartBid').value = latestHighestBid + 5000;
                            }
                        }
                    } else { dbInd.className = "w-2 h-2 rounded-full bg-rose-500"; dbTxt.className = "text-[10px] font-black text-slate-400 uppercase tracking-widest"; }

                    // Auction Status & Countdown Handling
                    if(data.auction) {
                        if(data.auction.timer_mode === 'AUTO') {
                            document.getElementById('timerText').innerText = `AUTO COUNTDOWN: ${data.auction.countdown}s`;
                        } else {
                            document.getElementById('timerText').innerText = `MODE: MANUAL CLOSE`;
                        }

                        if(data.auction.status === 'CLOSED') {
                            document.getElementById('modalWinner').innerText = data.market.winner_name || 'System';
                            document.getElementById('modalPrice').innerText = '₹' + (data.market.highest_bid || 0).toLocaleString('en-IN');
                            document.getElementById('winnerModal').classList.remove('hidden');
                        }
                    }

                    const locInd = document.getElementById('locustIndicator'); const locTxt = document.getElementById('locustStatusText');
                    if(data.locust_state === 'offline' || data.locust_state === 'stopped') {
                        locInd.className = "w-2 h-2 rounded-full bg-slate-600"; locTxt.className = "text-[10px] font-black text-slate-400 uppercase tracking-widest";
                        document.getElementById('rpsDisplay').innerText = "0.0"; 
                        document.getElementById('failDisplay').innerText = "0%";
                        locTxt.innerText = "TESTER IDLE";
                    } else {
                        locInd.className = "w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse shadow-[0_0_12px_#f43f5e]"; locTxt.className = "text-[10px] font-black text-rose-400 uppercase tracking-widest glow-red";
                        document.getElementById('rpsDisplay').innerText = data.rps; 
                        document.getElementById('failDisplay').innerText = data.failures + '%';
                        locTxt.innerText = "SWARM ACTIVE";
                    }
                    
                    const logContainer = document.getElementById('logContainer');
                    logContainer.innerHTML = data.logs.map(log => {
                        if(log.type === 'success') return `<div class="text-emerald-400 bg-emerald-500/10 px-2 py-1.5 rounded border border-emerald-500/20">${log.msg}</div>`;
                        return `<div class="text-rose-400 bg-rose-500/10 px-2 py-1.5 rounded border border-rose-500/20">${log.msg}</div>`;
                    }).join('');

                } catch(e) {}
            }
            setInterval(syncData, 1000);
        </script>
    </body>
    </html>
    """
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

load_dotenv()

# Global states
SWARM_CONFIG = {"starting_bid": 10000}
RECENT_LOGS = deque(maxlen=40)  # In-memory log buffer for the UI

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
            
            # Log Success
            log_msg = f"₹{bid.amount:,} | {bid.user_name} (Lock Secured)"
            RECENT_LOGS.appendleft({"type": "success", "msg": log_msg})
            print(f"\033[92m[VALID] {log_msg}\033[0m")
            return {"status": "Transaction Approved!"}
            
        except asyncpg.exceptions.SerializationError:
            # Log Race Condition Mitigation
            log_msg = f"₹{bid.amount:,} | {bid.user_name} (Race Mitigated)"
            RECENT_LOGS.appendleft({"type": "conflict", "msg": log_msg})
            print(f"\033[91m[BLOCKED] {log_msg}\033[0m")
            raise HTTPException(status_code=409, detail="Race condition mitigated.")

@app.post("/api/reset")
async def reset_auction():
    global SWARM_CONFIG, RECENT_LOGS
    SWARM_CONFIG["starting_bid"] = 5000
    RECENT_LOGS.clear()
    async with app.state.pool.acquire() as conn:
        await conn.execute("UPDATE final_auction SET highest_bid = 5000, winner_name = 'System' WHERE id = 1")
    return {"status": "Database Reset"}

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
    data = {"db_online": False, "locust_state": "offline", "rps": 0, "failures": 0, "market": {}, "logs": list(RECENT_LOGS)}
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
        <title>EpochBid | Live Demo</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800;900&family=JetBrains+Mono:wght@400;700;800&display=swap');
            body { font-family: 'Inter', sans-serif; background-color: #030712; color: #f8fafc; background-image: radial-gradient(circle at 50% 0%, rgba(34, 211, 238, 0.08), transparent 60%), linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px); background-size: 100% 100%, 40px 40px, 40px 40px; background-attachment: fixed; }
            .font-mono-num { font-family: 'JetBrains Mono', monospace; }
            .glass-panel { background: rgba(10, 15, 30, 0.7); backdrop-filter: blur(24px); border: 1px solid rgba(34, 211, 238, 0.15); box-shadow: inset 0 0 20px rgba(34, 211, 238, 0.02), 0 20px 40px rgba(0,0,0,0.6); }
            .glow-text { text-shadow: 0 0 30px rgba(34, 211, 238, 0.6), 0 0 10px rgba(255,255,255,0.8); }
            .glow-red { text-shadow: 0 0 20px rgba(244, 63, 94, 0.6); }
            ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 10px; }
            .tab-active { background-color: rgba(34, 211, 238, 0.1); color: #22d3ee; border-bottom: 2px solid #22d3ee; box-shadow: inset 0 -10px 15px -10px rgba(34,211,238,0.2); }
            .tab-inactive { color: #64748b; border-bottom: 2px solid transparent; }
        </style>
    </head>
    <body class="min-h-screen flex flex-col">
        
        <nav class="border-b border-slate-800/80 bg-slate-950/80 sticky top-0 z-50 backdrop-blur-xl">
            <div class="max-w-7xl mx-auto px-6 py-4 flex justify-between items-center">
                <div class="flex items-center gap-4">
                    <h1 class="text-2xl font-black tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-blue-500">EpochBid.</h1>
                    <div class="h-6 w-px bg-slate-700 mx-2"></div>
                    <span class="text-sm font-bold text-slate-400 tracking-widest uppercase hidden md:block">Live Engine</span>
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
            <div class="max-w-7xl mx-auto px-6 flex gap-2 mt-2">
                <button onclick="changeTab('market')" id="tab-market" class="tab-active px-8 py-3 text-sm font-bold transition-all rounded-t-xl uppercase tracking-wider">Market Dashboard</button>
                <button onclick="changeTab('trade')" id="tab-trade" class="tab-inactive px-8 py-3 text-sm font-bold transition-all rounded-t-xl uppercase tracking-wider">Manual Terminal</button>
            </div>
        </nav>

        <main class="flex-1 max-w-7xl mx-auto w-full px-6 py-6 relative">
            <!-- TAB 1: LIVE MARKET -->
            <div id="view-market" class="block animate-fade-in">
                <div class="glass-panel p-6 rounded-3xl flex flex-col min-h-[650px]">
                    
                    <!-- Header Stats -->
                    <div class="flex flex-col md:flex-row justify-between items-start mb-6">
                        <div>
                            <h2 class="text-xs text-slate-400 uppercase tracking-widest font-bold mb-2">Epoch GPU Cluster Valuation</h2>
                            <div class="text-[4rem] leading-none font-black text-white glow-text font-mono-num tracking-tight" id="priceDisplay">₹0</div>
                            <div class="text-sm text-slate-400 mt-4">Leading Bidder: <span id="winnerDisplay" class="text-cyan-400 font-bold px-3 py-1 bg-cyan-400/10 rounded-md border border-cyan-400/30 shadow-[0_0_10px_rgba(34,211,238,0.2)]">...</span></div>
                        </div>
                        <div class="flex gap-4 mt-4 md:mt-0">
                            <div class="bg-slate-900/60 border border-slate-700/50 p-4 rounded-2xl text-right min-w-[130px] shadow-lg backdrop-blur-md">
                                <div class="text-3xl font-black text-white font-mono-num" id="rpsDisplay">0.0</div>
                                <div class="text-[10px] text-cyan-500 uppercase tracking-widest mt-1 font-bold">Req / Sec</div>
                            </div>
                            <div class="bg-slate-900/60 border border-slate-700/50 p-4 rounded-2xl text-right min-w-[130px] shadow-lg backdrop-blur-md">
                                <div class="text-3xl font-black text-emerald-400 font-mono-num" id="failDisplay">0%</div>
                                <div class="text-[10px] text-emerald-500 uppercase tracking-widest mt-1 font-bold">Mitigated</div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Split Screen: Chart & Logs -->
                    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6 flex-1">
                        <!-- Chart Area -->
                        <div class="lg:col-span-2 bg-slate-950/80 border border-slate-800 rounded-2xl p-4 relative min-h-[300px] shadow-inner">
                            <canvas id="liveChart"></canvas>
                        </div>
                        
                        <!-- Live Logging Feed -->
                        <div class="bg-slate-950/80 border border-slate-800 rounded-2xl p-4 shadow-inner flex flex-col h-[300px] lg:h-auto">
                            <h3 class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-3 flex items-center gap-2">
                                <div class="w-1.5 h-1.5 rounded-full bg-cyan-500 animate-pulse"></div> Live Transaction Feed
                            </h3>
                            <div id="logContainer" class="flex-1 overflow-y-auto space-y-2 pr-2 font-mono-num text-xs flex flex-col">
                                <!-- Logs injected here -->
                            </div>
                        </div>
                    </div>
                    
                    <!-- Labeled Swarm Controls -->
                    <div class="grid grid-cols-1 md:grid-cols-5 gap-4 bg-slate-900/40 p-5 rounded-2xl border border-slate-700/50 backdrop-blur-md">
                        <div>
                            <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2 ml-1">Swarm Users</label>
                            <input type="number" id="testUsers" value="100" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-4 py-3 text-sm text-center outline-none focus:border-cyan-400 transition font-mono-num">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2 ml-1">Spawn Rate</label>
                            <input type="number" id="testRate" value="20" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-4 py-3 text-sm text-center outline-none focus:border-cyan-400 transition font-mono-num">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-cyan-400 uppercase tracking-widest mb-2 ml-1">Target Bid (₹)</label>
                            <input type="number" id="testStartBid" value="10000" class="w-full bg-slate-950/80 border border-slate-600 rounded-xl px-4 py-3 text-sm text-center outline-none focus:border-cyan-400 transition font-bold text-cyan-400 font-mono-num shadow-[0_0_10px_rgba(34,211,238,0.1)]">
                        </div>
                        <div class="flex items-end">
                            <button onclick="startTest()" class="w-full bg-gradient-to-r from-rose-600 to-rose-500 text-white border border-rose-400/50 hover:from-rose-500 hover:to-rose-400 transition-all rounded-xl font-black text-xs tracking-widest uppercase py-3.5 shadow-[0_0_20px_rgba(225,29,72,0.4)]">Ignite Swarm</button>
                        </div>
                        <div class="flex items-end">
                            <button onclick="stopTest()" class="w-full bg-slate-800 text-slate-300 hover:bg-slate-700 hover:text-white border border-slate-600 transition-all rounded-xl font-bold text-xs tracking-widest uppercase py-3.5">Halt Traffic</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TAB 2: MANUAL TRADE -->
            <div id="view-trade" class="hidden animate-fade-in max-w-xl mx-auto mt-10">
                <div class="glass-panel p-10 rounded-3xl border-t-4 border-t-emerald-400">
                    <h2 class="text-2xl font-black mb-2 text-white tracking-tight">Execute Manual Trade</h2>
                    <p class="text-sm text-slate-400 mb-8 font-medium">Inject a manual transaction alongside the locust swarm.</p>
                    <div class="space-y-6">
                        <div>
                            <label class="block text-xs font-bold text-emerald-500 uppercase tracking-widest mb-2">Trader Identity</label>
                            <input type="text" id="userName" placeholder="e.g. Sourish_Alpha" class="w-full bg-slate-950/80 border border-slate-700 rounded-xl px-5 py-4 text-sm focus:outline-none focus:border-emerald-400 shadow-inner font-mono-num">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-emerald-500 uppercase tracking-widest mb-2">Bid Amount (₹)</label>
                            <input type="number" id="bidAmount" placeholder="0" class="w-full bg-slate-950/80 border border-slate-700 rounded-xl px-5 py-4 text-sm focus:outline-none focus:border-emerald-400 shadow-inner font-mono-num">
                        </div>
                        <button onclick="placeBid()" class="w-full mt-6 bg-gradient-to-r from-emerald-500 to-emerald-400 hover:from-emerald-400 hover:to-emerald-300 text-slate-950 font-black tracking-widest uppercase py-4 rounded-xl shadow-[0_0_25px_rgba(16,185,129,0.4)] transition-all">Submit Transaction</button>
                        <div id="bidStatusMsg" class="text-center text-sm font-bold h-6 mt-4 opacity-0 transition-opacity"></div>
                    </div>
                </div>
            </div>
        </main>

        <script>
            let chartInstance = null;
            const ctx = document.getElementById('liveChart').getContext('2d');
            let gradient = ctx.createLinearGradient(0, 0, 0, 400);
            gradient.addColorStop(0, 'rgba(34, 211, 238, 0.4)'); gradient.addColorStop(1, 'rgba(34, 211, 238, 0.0)');

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [{ data: [], borderColor: '#22d3ee', backgroundColor: gradient, borderWidth: 3, fill: true, tension: 0.4, pointRadius: 0, pointHoverRadius: 6 }] },
                options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 }, scales: { x: { display: false }, y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b', font: {family: 'JetBrains Mono'}, callback: function(value) { return '₹' + value.toLocaleString('en-IN'); } } } }, plugins: { legend: { display: false } } }
            });

            function changeTab(tabName) {
                ['market', 'trade'].forEach(t => {
                    document.getElementById(`view-${t}`).classList.add('hidden'); document.getElementById(`view-${t}`).classList.remove('block');
                    document.getElementById(`tab-${t}`).classList.remove('tab-active'); document.getElementById(`tab-${t}`).classList.add('tab-inactive');
                });
                document.getElementById(`view-${tabName}`).classList.remove('hidden'); document.getElementById(`view-${tabName}`).classList.add('block');
                document.getElementById(`tab-${tabName}`).classList.remove('tab-inactive'); document.getElementById(`tab-${tabName}`).classList.add('tab-active');
            }

            async function resetSystem() {
                if(!confirm("Are you sure you want to HARD RESET the database and clear all logs?")) return;
                await fetch('/api/reset', { method: 'POST' });
                chartInstance.data.labels = [];
                chartInstance.data.datasets[0].data = [];
                chartInstance.update();
                document.getElementById('testStartBid').value = 10000;
            }

            async function placeBid() {
                const user_name = document.getElementById('userName').value;
                const amount = parseInt(document.getElementById('bidAmount').value);
                const stat = document.getElementById('bidStatusMsg');
                try {
                    const res = await fetch('/bid', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_name, amount}) });
                    const data = await res.json();
                    stat.innerText = res.ok ? "Transaction Approved!" : data.detail;
                    stat.className = `text-center text-sm font-bold h-6 mt-4 opacity-100 ${res.ok ? 'text-emerald-400 glow-text' : 'text-rose-500 glow-red'}`;
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
                        dbInd.className = "w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse shadow-[0_0_10px_#34d399]"; dbTxt.className = "text-[10px] font-black text-emerald-400 uppercase tracking-widest";
                        if(data.market && Object.keys(data.market).length !== 0) {
                            document.getElementById('priceDisplay').innerText = '₹' + data.market.highest_bid.toLocaleString('en-IN');
                            document.getElementById('winnerDisplay').innerText = data.market.winner_name;
                            
                            const time = new Date().toLocaleTimeString();
                            chartInstance.data.labels.push(time);
                            chartInstance.data.datasets[0].data.push(data.market.highest_bid);
                            if(chartInstance.data.labels.length > 40) { chartInstance.data.labels.shift(); chartInstance.data.datasets[0].data.shift(); }
                            chartInstance.update('none');
                            
                            if (data.locust_state === 'offline' && !document.activeElement.id.includes('testStartBid')) {
                                document.getElementById('testStartBid').value = data.market.highest_bid + 5000;
                            }
                        }
                    } else { dbInd.className = "w-2.5 h-2.5 rounded-full bg-rose-500"; dbTxt.className = "text-[10px] font-black text-slate-400 uppercase tracking-widest"; }

                    const locInd = document.getElementById('locustIndicator'); const locTxt = document.getElementById('locustStatusText');
                    if(data.locust_state === 'offline') {
                        locInd.className = "w-2.5 h-2.5 rounded-full bg-slate-600"; locTxt.className = "text-[10px] font-black text-slate-400 uppercase tracking-widest";
                        document.getElementById('rpsDisplay').innerText = "0.0"; document.getElementById('failDisplay').innerText = "0%";
                    } else {
                        locInd.className = "w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse shadow-[0_0_15px_#f43f5e]"; locTxt.className = "text-[10px] font-black text-rose-400 uppercase tracking-widest glow-red";
                        document.getElementById('rpsDisplay').innerText = data.rps; document.getElementById('failDisplay').innerText = data.failures + '%';
                    }
                    
                    // Render Logs
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
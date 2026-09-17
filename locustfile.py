import requests
from locust import HttpUser, task, between

# Global atomic state shared across ALL bots in the swarm
GLOBAL_BID = 0

class HackathonStressTest(HttpUser):
    # Extremely aggressive wait time to intentionally trigger race conditions
    wait_time = between(0.01, 0.05)

    def on_start(self):
        global GLOBAL_BID
        try:
            # Fetch custom start bid from the UI when you click "Ignite"
            res = requests.get("http://127.0.0.1:8000/api/locust/config")
            target_bid = res.json().get("starting_bid", 10000)
            
            # Sync the swarm to the new UI configuration
            if target_bid > GLOBAL_BID:
                GLOBAL_BID = target_bid
        except:
            if GLOBAL_BID == 0:
                GLOBAL_BID = 10000

    @task
    def simulate_bid(self):
        global GLOBAL_BID
        
        # 1. Atomically push the global swarm price up
        GLOBAL_BID += 100
        my_bid = GLOBAL_BID

        # 2. Fire the transaction at the backend
        with self.client.post("/bid", json={
            "user_name": f"SwarmBot_{my_bid}",
            "amount": my_bid
        }, catch_response=True) as response:
            
            # 3. Grade the system based on your Hackathon Rubric
            if response.status_code == 200:
                response.success() # Valid transaction accepted
            elif response.status_code == 409:
                response.success() # DB Lock mitigated a race condition (Perfect score)
            elif response.status_code == 400:
                response.success() # Out-of-order bid correctly rejected (Perfect score)
            else:
                response.failure(f"System Crash: {response.status_code}")
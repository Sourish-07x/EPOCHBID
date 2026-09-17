# EpochBid: High-Frequency Bidding & Auction Engine

EpochBid is a high-throughput, low-latency financial auction and bidding platform designed for high-concurrency transaction loads. It ensures data integrity, strict bid serialization, and atomic transactions under heavy race condition stress.

## Team
* **Sourish & Shreyan** — Backend Architecture & Concurrency Control
* **Ananya** — Technical Documentation
* **Ishaan** — Presentation Architecture
* **Souptik** — Version Control & GitHub Operations

## Tech Stack
* **Backend:** Python, FastAPI, Uvicorn
* **Database:** PostgreSQL (Neon.db via AsyncPG, Serializable Isolation)
* **Load Testing:** Locust
* **Networking:** Localtunnel, HTTPX
* **Frontend:** Tailwind CSS, Chart.js

## Core Engineering Requirements
1. **Bid Serialization:** Rejects bids lower than or equal to the current highest bid to prevent out-of-order execution.
2. **ACID Compliance:** Utilizes PostgreSQL `Serializable` transaction isolation to prevent race conditions, ghost bids, and double allocations.
3. **Low-Latency Telemetry:** Real-time metrics broadcast paired with an automated idle countdown timer and winner verification modal.
4. **Stress Testing Integration:** Integrated Locust swarm ignition for live multi-client benchmarking.

## Engineering Challenges & Solutions

### 1. Concurrency Race Conditions
* **Problem:** Concurrent virtual users reading the same highest bid simultaneously caused price overrides and out-of-order execution.
* **Solution:** Implemented explicit PostgreSQL transactions under `Serializable Isolation`. Conflicting transactions trigger an `asyncpg.exceptions.SerializationError`, which is handled and logged as a mitigated race condition.

### 2. Locust State Retention
* **Problem:** Halting and restarting load tests retained old memory counters, causing new tests to resume at inflated baseline prices.
* **Solution:** Added a Locust `@events.test_start` hook that queries `/api/locust/config` on test start to reset swarm baseline pricing.

### 3. Asynchronous Telemetry Polling
* **Problem:** Rapid state updates caused metrics like RPS and failure rates to lock up during idle periods.
* **Solution:** Developed an asynchronous background polling loop (`syncData()`) running every second to cleanly separate load test metrics from manual transaction streams.

## Local Setup

1. **Clone Repository:**
   ```bash
   git clone [https://github.com/your-username/EpochBid.git](https://github.com/your-username/EpochBid.git)
   cd EpochBid

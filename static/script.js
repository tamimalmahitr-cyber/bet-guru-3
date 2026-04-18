(function () {
    const socket = io();
    const state = {
        user: null,
        table: {
            state: "WAITING",
            multiplier: 1,
            countdown: 10,
            history: [],
            players: [],
        },
    };

    const els = {
        joinModal: document.getElementById("join-modal"),
        usernameInput: document.getElementById("username-input"),
        joinBtn: document.getElementById("join-btn"),
        usernameDisplay: document.getElementById("username-display"),
        balanceDisplay: document.getElementById("balance-display"),
        roundState: document.getElementById("round-state"),
        countdownDisplay: document.getElementById("countdown-display"),
        seedHash: document.getElementById("seed-hash"),
        multiplierDisplay: document.getElementById("multiplier-display"),
        multiplierSubtext: document.getElementById("multiplier-subtext"),
        placeBetBtn: document.getElementById("place-bet-btn"),
        cashOutBtn: document.getElementById("cash-out-btn"),
        betAmount: document.getElementById("bet-amount"),
        autoCashout: document.getElementById("auto-cashout"),
        noticeBar: document.getElementById("notice-bar"),
        activeBetDisplay: document.getElementById("active-bet-display"),
        activeAutoDisplay: document.getElementById("active-auto-display"),
        playerList: document.getElementById("player-list"),
        historyChart: document.getElementById("history-chart"),
        historyPills: document.getElementById("history-pills"),
        resultToast: document.getElementById("result-toast"),
        planeWrapper: document.getElementById("plane-wrapper"),
        explosion: document.getElementById("explosion"),
        flightStage: document.getElementById("flight-stage"),
    };

    function setNotice(message) {
        els.noticeBar.textContent = message;
    }

    function showToast(message, type) {
        els.resultToast.textContent = message;
        els.resultToast.className = `toast ${type || ""}`;
        setTimeout(() => {
            els.resultToast.className = "toast hidden";
        }, 2800);
    }

    function renderUser() {
        if (!state.user) return;
        els.usernameDisplay.textContent = state.user.username;
        els.balanceDisplay.textContent = Number(state.user.balance || 0).toFixed(2);

        const active = state.user.active_bet;
        if (!active || active.result === "lost" || active.result === "cashed_out") {
            els.activeBetDisplay.textContent = "None";
            els.activeAutoDisplay.textContent = "--";
        } else {
            els.activeBetDisplay.textContent = `${Number(active.amount).toFixed(2)} credits`;
            els.activeAutoDisplay.textContent = active.auto_cashout ? `${Number(active.auto_cashout).toFixed(2)}x` : "Manual";
        }

        const hasPendingBet = active && active.result === "pending";
        const canBet = state.table.state === "STARTING" && !hasPendingBet;
        const canCashOut = state.table.state === "RUNNING" && hasPendingBet;
        els.placeBetBtn.disabled = !canBet;
        els.cashOutBtn.disabled = !canCashOut;
    }

    function renderPlayers(players) {
        if (!players || players.length === 0) {
            els.playerList.innerHTML = '<div class="player-item"><div><strong>No bets yet</strong><small>Waiting for players to join this round.</small></div><span>--</span></div>';
            return;
        }

        els.playerList.innerHTML = players.map((player) => {
            const status = player.result === "cashed_out"
                ? `Cashed out @ ${Number(player.cashout_multiplier || 0).toFixed(2)}x`
                : player.result;
            return `
                <div class="player-item">
                    <div>
                        <strong>${player.username}</strong>
                        <small>${Number(player.amount).toFixed(2)} credits</small>
                    </div>
                    <span>${status}</span>
                </div>
            `;
        }).join("");
    }

    function renderHistory(history) {
        const items = history || [];
        els.historyPills.innerHTML = "";
        els.historyChart.innerHTML = "";

        if (items.length === 0) {
            els.historyChart.innerHTML = '<div class="player-item"><div><strong>No finished rounds yet</strong><small>Crash history will appear here.</small></div></div>';
            return;
        }

        items.slice().reverse().forEach((item) => {
            const crash = Number(item.crash_point || 1);
            const bar = document.createElement("div");
            const percentage = Math.max(12, Math.min(100, (crash / 20) * 100));
            let tone = "";
            if (crash < 2) tone = "low";
            if (crash >= 2 && crash < 5) tone = "mid";
            bar.className = `history-bar ${tone}`;
            bar.style.height = `${percentage}%`;
            bar.textContent = `${crash.toFixed(2)}x`;
            els.historyChart.appendChild(bar);
        });

        items.forEach((item) => {
            const pill = document.createElement("div");
            pill.className = "history-pill";
            pill.textContent = `${Number(item.crash_point || 1).toFixed(2)}x`;
            els.historyPills.appendChild(pill);
        });
    }

    function updateFlight(multiplier, crashed) {
        const climb = Math.min(250, (Math.max(multiplier, 1) - 1) * 32);
        const drift = Math.min(460, (Math.max(multiplier, 1) - 1) * 55);
        const translateY = crashed ? climb - 24 : climb;
        els.planeWrapper.style.transform = `translate(${drift}px, -${translateY}px) rotate(-8deg)`;

        if (crashed) {
            const rect = els.planeWrapper.getBoundingClientRect();
            const stageRect = els.flightStage.getBoundingClientRect();
            els.explosion.style.left = `${rect.left - stageRect.left - 12}px`;
            els.explosion.style.top = `${rect.top - stageRect.top - 10}px`;
            els.explosion.classList.remove("active");
            void els.explosion.offsetWidth;
            els.explosion.classList.add("active");
        }
    }

    function renderTable() {
        const table = state.table;
        els.roundState.textContent = table.state;
        els.countdownDisplay.textContent = table.state === "STARTING" ? table.countdown : "--";
        els.seedHash.textContent = table.seed_hash ? `${table.seed_hash.slice(0, 16)}...` : "--";
        els.multiplierDisplay.textContent = `${Number(table.multiplier || 1).toFixed(2)}x`;

        if (table.state === "STARTING") {
            els.multiplierSubtext.textContent = `Betting closes in ${table.countdown}s`;
            updateFlight(1, false);
        } else if (table.state === "RUNNING") {
            els.multiplierSubtext.textContent = "Rocket is climbing. Cash out before the crash.";
            updateFlight(table.multiplier || 1, false);
        } else if (table.state === "CRASHED") {
            els.multiplierSubtext.textContent = `Crashed at ${Number(table.crash_point || table.multiplier || 1).toFixed(2)}x`;
            updateFlight(table.crash_point || table.multiplier || 1, true);
        } else {
            els.multiplierSubtext.textContent = "Waiting for the next launch";
            updateFlight(1, false);
        }

        renderPlayers(table.players || []);
        renderHistory(table.history || []);
        renderUser();
    }

    function joinGame() {
        const username = els.usernameInput.value.trim();
        if (!username) {
            setNotice("Please enter a username first.");
            return;
        }
        socket.emit("join_game", { username });
    }

    els.joinBtn.addEventListener("click", joinGame);
    els.usernameInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            joinGame();
        }
    });

    els.placeBetBtn.addEventListener("click", () => {
        const amount = Number(els.betAmount.value || 0);
        const autoCashoutRaw = els.autoCashout.value.trim();
        socket.emit("place_bet", {
            amount,
            auto_cashout: autoCashoutRaw ? Number(autoCashoutRaw) : null,
        });
    });

    els.cashOutBtn.addEventListener("click", () => {
        socket.emit("cash_out");
    });

    socket.on("connected", () => {
        setNotice("Connected. Choose a username to join the table.");
    });

    socket.on("joined_game", (payload) => {
        state.user = payload.user || null;
        els.joinModal.style.display = "none";
        setNotice("Joined the game. Place a bet during the countdown.");
        renderUser();
    });

    socket.on("player_state", (payload) => {
        state.user = payload;
        renderUser();
    });

    socket.on("table_state", (payload) => {
        state.table = { ...state.table, ...payload };
        renderTable();
    });

    socket.on("countdown_timer", (payload) => {
        state.table.state = "STARTING";
        state.table.countdown = payload.seconds_left;
        state.table.seed_hash = payload.seed_hash;
        renderTable();
    });

    socket.on("round_start", (payload) => {
        state.table.state = "RUNNING";
        state.table.multiplier = 1;
        state.table.seed_hash = payload.seed_hash;
        setNotice("Round started. Cash out before the crash.");
        renderTable();
    });

    socket.on("multiplier_update", (payload) => {
        state.table.state = "RUNNING";
        state.table.multiplier = payload.value;
        renderTable();
    });

    socket.on("round_crash", (payload) => {
        state.table.state = "CRASHED";
        state.table.crash_point = payload.crash_point;
        state.table.multiplier = payload.crash_point;
        setNotice(`Round crashed at ${Number(payload.crash_point).toFixed(2)}x`);
        renderTable();
    });

    socket.on("live_players", (payload) => {
        state.table.players = payload.players || [];
        renderPlayers(state.table.players);
        renderUser();
    });

    socket.on("round_history", (payload) => {
        state.table.history = payload.items || [];
        renderHistory(state.table.history);
    });

    socket.on("bet_placed", (payload) => {
        setNotice(payload.message || "Bet accepted.");
        showToast(payload.message || "Bet accepted.", "win");
    });

    socket.on("player_cashout_result", (payload) => {
        setNotice(payload.message || "Cash out complete.");
        showToast(payload.message || "Cash out complete.", "win");
    });

    socket.on("bet_result", (payload) => {
        const type = payload.status === "win" ? "win" : "loss";
        showToast(payload.message || "Round settled.", type);
    });

    socket.on("server_error", (payload) => {
        setNotice(payload.message || "Server error.");
        showToast(payload.message || "Server error.", "loss");
    });

    renderTable();
})();

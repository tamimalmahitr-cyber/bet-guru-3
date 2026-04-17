(function () {
    const config = window.GAME_PAGE_CONFIG;
    const root = document.querySelector("[data-game-page]");
    if (!config || !root) return;

    const socket = io();
    const els = {
        phase: document.getElementById("phase-label"),
        wallet: document.getElementById("wallet-balance"),
        players: document.getElementById("players-list"),
        playerCount: document.getElementById("player-count"),
        primary: document.getElementById("primary-result"),
        secondary: document.getElementById("secondary-result"),
        status: document.getElementById("status-text"),
        countdown: document.getElementById("countdown"),
        betForm: document.getElementById("bet-form"),
        actionMessage: document.getElementById("action-message"),
        cashoutButton: document.getElementById("cashout-button"),
        recentHistory: document.getElementById("recent-history"),
        myHistory: document.getElementById("my-history"),
        rocketShip: document.getElementById("rocket-ship"),
        liveMultiplier: document.getElementById("live-multiplier"),
        wheel: document.getElementById("fortune-wheel"),
        derbyTrack: document.getElementById("derby-track"),
        dieOne: document.getElementById("die-one"),
        dieTwo: document.getElementById("die-two"),
    };

    const parseHistory = (node) => {
        if (!node) return [];
        try {
            return JSON.parse(node.dataset.history || "[]");
        } catch {
            return [];
        }
    };

    let state = config.initialState || {};
    let recentHistory = parseHistory(els.recentHistory);
    let myHistory = parseHistory(els.myHistory);

    function showMessage(message, type) {
        if (!els.actionMessage) return;
        els.actionMessage.textContent = message || "";
        els.actionMessage.style.color = type === "error" ? "#f87171" : "#fbbf24";
    }

    function renderPlayers(players) {
        if (!els.players) return;
        if (!players || !players.length) {
            els.players.innerHTML = '<div class="player-card"><strong>No bets yet</strong><span>Be the first one in this round.</span></div>';
            return;
        }
        els.players.innerHTML = players.map((player) => `
            <div class="player-card">
                <strong>${player.username}</strong>
                <span>${player.amount} pts on ${player.choice}</span>
                <span>Status: ${player.status}</span>
            </div>
        `).join("");
    }

    function renderHistory(node, entries) {
        if (!node) return;
        if (!entries || !entries.length) {
            node.innerHTML = '<div class="history-item"><strong>No settled rounds yet</strong><span>Results will appear here.</span></div>';
            return;
        }
        node.innerHTML = entries.map((entry) => `
            <div class="history-item ${entry.outcome}">
                <strong>${entry.outcome.replace("_", " ")}</strong>
                <span>Bet ${entry.amount} pts -> Payout ${entry.payout} pts</span>
                <span>${entry.created_at || ""}</span>
            </div>
        `).join("");
    }

    function renderDerbyPositions(positions) {
        if (!els.derbyTrack) return;
        const horses = positions || {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0};
        els.derbyTrack.innerHTML = Object.entries(horses).map(([horse, pos]) => `
            <div class="track-lane">
                <div class="track-horse" style="width:${Math.max(12, Number(pos))}%">#${horse}</div>
            </div>
        `).join("");
    }

    function renderVisual(payload) {
        const phase = payload.phase || "booting";
        const gameState = payload.state || {};
        if (els.phase) els.phase.textContent = phase;
        if (els.wallet && typeof payload.wallet_balance !== "undefined") els.wallet.textContent = payload.wallet_balance;
        if (els.status) els.status.textContent = gameState.status_text || payload.status_text || "Round update";
        if (els.countdown) els.countdown.textContent = payload.countdown || "-";
        if (els.playerCount) els.playerCount.textContent = payload.player_count || 0;
        renderPlayers(payload.players || []);

        if (config.slug === "neon-rocket") {
            const multiplier = payload.live_multiplier || gameState.multiplier || 1.0;
            if (els.primary) els.primary.textContent = `${Number(multiplier).toFixed(2)}x`;
            if (els.liveMultiplier) els.liveMultiplier.textContent = `${Number(multiplier).toFixed(2)}x`;
            if (els.secondary) {
                els.secondary.textContent = phase === "result"
                    ? `Crash point: ${Number((payload.result && payload.result.crash_point) || gameState.crash_point || 1).toFixed(2)}x`
                    : "Cash out before the server crash point.";
            }
            if (els.rocketShip) {
                const lift = Math.min((Number(multiplier) - 1) * 28, 190);
                els.rocketShip.style.transform = `translateY(-${lift}px)`;
            }
        } else if (config.slug === "color-wheel") {
            const angle = (payload.result && payload.result.wheel_angle) || gameState.wheel_angle || 0;
            if (els.wheel) els.wheel.style.transform = `rotate(${angle}deg)`;
            if (els.primary) els.primary.textContent = (payload.result && payload.result.winning_color) || gameState.winning_color || "Pending";
            if (els.secondary && payload.result) els.secondary.textContent = payload.result.status_text || "";
        } else if (config.slug === "cyber-derby") {
            renderDerbyPositions((payload.result && payload.result.positions) || gameState.positions);
            if (els.primary) els.primary.textContent = (payload.result && payload.result.winner) || gameState.winner || "Pending";
            if (els.secondary && payload.result) els.secondary.textContent = payload.result.status_text || "";
        } else if (config.slug === "dice-duel") {
            const dice = (payload.result && payload.result.dice) || gameState.dice || [1, 1];
            if (els.dieOne) els.dieOne.textContent = dice[0];
            if (els.dieTwo) els.dieTwo.textContent = dice[1];
            if (els.primary) {
                els.primary.textContent = payload.result
                    ? `${payload.result.winning_side} (${payload.result.sum})`
                    : `${(gameState.dice || [1, 1]).join(" + ")}`;
            }
            if (els.secondary && payload.result) els.secondary.textContent = payload.result.status_text || "";
        }

        if (els.cashoutButton) {
            const shouldShowCashout = config.supportsCashout && phase === "running";
            els.cashoutButton.classList.toggle("d-none", !shouldShowCashout);
        }
    }

    async function refreshSnapshot() {
        const response = await fetch(`/api/realtime/${config.slug}/state`);
        const data = await response.json();
        if (data.ok) {
            state = data.state;
            renderVisual(state);
        }
    }

    async function submitBet(event) {
        event.preventDefault();
        const formData = new FormData(els.betForm);
        const payload = {
            amount: formData.get("amount"),
            choice: formData.get("choice"),
        };
        const response = await fetch(`/api/realtime/${config.slug}/bet`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        showMessage(data.message, data.ok ? "ok" : "error");
        if (typeof data.balance !== "undefined" && els.wallet) {
            els.wallet.textContent = data.balance;
        }
        if (data.history) {
            myHistory = data.history;
            renderHistory(els.myHistory, myHistory);
        }
    }

    async function cashout() {
        const response = await fetch("/api/realtime/neon-rocket/cashout", {method: "POST"});
        const data = await response.json();
        showMessage(data.message, data.ok ? "ok" : "error");
        if (typeof data.balance !== "undefined" && els.wallet) {
            els.wallet.textContent = data.balance;
        }
    }

    socket.on("connect", () => {
        socket.emit("join_game", {game: config.slug});
    });

    socket.on("round_state", (payload) => {
        if (!payload || payload.game_slug !== config.slug) return;
        state = payload;
        renderVisual(payload);
    });

    socket.on("bet_update", (payload) => {
        if (!payload || payload.game_slug !== config.slug) return;
        state = payload;
        renderVisual(payload);
    });

    socket.on("cashout_update", (payload) => {
        if (!payload || payload.game_slug !== config.slug) return;
        state = payload;
        renderVisual(payload);
    });

    socket.on("wallet_update", (payload) => {
        if (els.wallet && typeof payload.balance !== "undefined") {
            els.wallet.textContent = payload.balance;
        }
    });

    if (els.betForm) {
        els.betForm.addEventListener("submit", submitBet);
    }

    if (els.cashoutButton) {
        els.cashoutButton.addEventListener("click", cashout);
    }

    renderVisual(state);
    renderHistory(els.recentHistory, recentHistory);
    renderHistory(els.myHistory, myHistory);
    if (config.slug === "cyber-derby") {
        renderDerbyPositions((state.state && state.state.positions) || null);
    }
    refreshSnapshot().catch(() => null);
})();

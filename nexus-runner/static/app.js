/* Nexus Runner -- Web dashboard client */

let ws;
let reconnectTimer = null;
let currentAvatar = "nexus";

// Tool metadata for the tools tab
const TOOL_META = {
    get_time: { desc: "Get the current date and time", params: [] },
    set_reminder: { desc: "Add a reminder surfaced contextually", params: ["trigger", "content", "expires"] },
    add_calendar_event: { desc: "Create an event in Apple Calendar", params: ["title", "date", "time", "duration_minutes"] },
    update_soul: { desc: "Update persistent memory about the user", params: ["section", "content"] },
    run_command: { desc: "Run an allowlisted shell command", params: ["command"] },
    open_app: { desc: "Open a macOS application by name", params: ["app_name"] },
    schedule_task: { desc: "Schedule a command to run at a specific time", params: ["name", "command", "when", "repeat"] },
    list_scheduled_tasks: { desc: "List all scheduled tasks", params: [] },
    remove_scheduled_task: { desc: "Remove a scheduled task by name", params: ["name"] },
    run_code: { desc: "Execute Python or bash code and return output", params: ["language", "code"] },
    create_skill: { desc: "Create a reusable skill (saved script)", params: ["name", "description", "language", "code"] },
    run_skill: { desc: "Run a previously saved skill by name", params: ["name", "args"] },
    list_skills: { desc: "List all saved skills", params: [] },
    edit_soul: { desc: "Replace the entire soul.md file", params: ["content"] },
    edit_settings: { desc: "Modify agent settings (voice, rate, etc)", params: ["setting", "value"] },
    search_spotify: { desc: "Search Spotify for songs, artists, albums, playlists", params: ["query", "type"] },
    search_instagram: { desc: "Search Instagram profiles, hashtags, or content", params: ["query", "type"] },
    browse: { desc: "Control Brave browser (navigate, click, fill, scroll, read, screenshot)", params: ["action", "url", "selector", "value", "direction"] },
};

// Markdown setup
marked.setOptions({
    highlight: function (code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
    breaks: true,
});

// ── WebSocket ──

function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    // Close any existing socket before reconnecting
    if (ws) {
        try { ws.onclose = null; ws.close(); } catch (e) {}
        ws = null;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
        console.log("Connected to Nexus Runner");
        const st = document.getElementById("status-text");
        if (st) { st.textContent = "online"; st.classList.add("online"); }
    };

    ws.onclose = () => {
        console.log("Disconnected, reconnecting in 2s...");
        const st = document.getElementById("status-text");
        if (st) { st.textContent = "offline"; st.classList.remove("online"); }
        reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
        try { ws.close(); } catch (e) {}
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleMessage(data);
    };
}

document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (!ws || ws.readyState > 1)) {
        connect();
    }
});

function send(data) {
    if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify(data));
    }
}

// ── Message handlers ──

function handleMessage(data) {
    switch (data.type) {
        case "init":
            initState(data);
            break;
        case "message":
            removeThinking();
            addMessage(data.role, data.content, data.name);
            if (data.role === "assistant") {
                setBeacon(data.voice ? "speaking" : "idle");
                // Always unmute if voice is off (in case wake word set wakeMuted=true)
                if (!data.voice) {
                    wakeMuted = false;
                }
                // Mute wake listener while clippy speaks so he doesn't hear himself
                if (data.voice) {
                    wakeMuted = true;
                    lastAssistantText = (data.content || "").toLowerCase().trim();
                    const words = (data.content || "").split(/\s+/).length;
                    // Rate 200 = ~200 wpm = 300ms/word. Add 6s buffer for echo/reverb.
                    const rate = parseInt(document.getElementById("setting-rate")?.value) || 200;
                    const msPerWord = Math.max(200, 60000 / rate);
                    const speakTime = Math.min(60000, Math.max(5000, words * msPerWord + 6000));
                    console.log(`[wake] Muting for ${speakTime}ms (${words} words @ rate ${rate})`);
                    setTimeout(() => {
                        wakeMuted = false;
                        // Stay in conversation mode -- keep listening after response
                        if (wakeConversationMode) {
                            setBeacon("listening");
                            console.log("[wake] Unmuted, still in conversation");
                        } else {
                            setBeacon("idle");
                            console.log("[wake] Unmuted, back to idle");
                        }
                        // Clear echo text after a beat so it doesn't block real input
                        setTimeout(() => { lastAssistantText = ""; }, 3000);
                    }, speakTime);
                }
            }
            break;
        case "thinking":
            setBeacon("thinking");
            playThinkSound();
            showThinking();
            break;
        case "soul_saved":
            flashStatus("soul-status", "Saved");
            break;
        case "settings_saved":
            flashStatus("settings-status", "Saved");
            // Update status bar
            if (data.settings) {
                (document.getElementById("status-voice")||{}).textContent = data.settings.voice || "--";
            }
            break;
        case "reminders_saved":
            flashStatus("reminders-status", "Saved");
            break;
        case "avatar_switched":
            currentAvatar = data.avatar;
            updateAvatarDisplay(data.avatar);
            (document.getElementById("status-voice")||{}).textContent = data.settings.voice;
            populateVoiceDropdown(data.voices || [], data.settings.voice);
            initWakeListener(data);
            renderUserAvatarPicker();
            addSystemMessage(`Avatar switched to ${data.avatar}`);
            break;
        case "queued":
            showQueuedIndicator();
            break;
        case "interrupted":
            removeThinking();
            removeQueuedIndicator();
            addSystemMessage("Interrupted");
            break;
        case "history_cleared":
            document.getElementById("messages").innerHTML = "";
            messageCount = 0;
            addSystemMessage("History cleared");
            break;
        case "new_session_started":
            document.getElementById("messages").innerHTML = "";
            messageCount = 0;
            currentSessionId = null;
            addSystemMessage("New session");
            break;
        case "skills_list":
            renderSkills(data.skills || []);
            break;
        case "skill_saved":
            flashStatus("skills-status", "Saved");
            send({ type: "list_skills" });
            closeSkillEditor();
            break;
        case "skill_deleted":
            flashStatus("skills-status", "Deleted");
            send({ type: "list_skills" });
            closeSkillEditor();
            break;
        case "skill_output":
            addSystemMessage(`Skill output: ${data.output}`);
            break;
        case "skill_detail":
            openSkillEditor(data.skill);
            break;
        case "sessions_list":
            renderSessions(data.sessions || []);
            break;
        case "session_saved":
            if (data.session) currentSessionId = data.session.id;
            send({ type: "list_sessions" });
            break;
        case "session_autosaved":
            if (data.session) currentSessionId = data.session.id;
            if (data.sessions) renderSessions(data.sessions);
            break;
        case "reminders_updated":
            renderReminders(data.reminders || []);
            break;
        case "scheduled_tasks":
            renderScheduledTasks(data.tasks || []);
            break;
        case "task_added":
            flashStatus("schedule-status", data.result || "Added");
            send({ type: "list_scheduled_tasks" });
            break;
        case "task_removed":
            flashStatus("schedule-status", data.result || "Removed");
            send({ type: "list_scheduled_tasks" });
            break;
        case "session_loaded":
            currentSessionId = data.session.id;
            messageCount = 0;
            document.getElementById("messages").innerHTML = "";
            (data.history || []).forEach((msg) => addMessage(msg.role, msg.content));
            addSystemMessage(`Loaded: ${data.session.name}`);
            send({ type: "list_sessions" });
            break;
        case "memory_saved":
            flashStatus("memory-status", "Saved");
            if (data.memory != null) {
                renderMemories(data.memory);
                const memEditor = document.getElementById("memory-editor");
                if (memEditor) memEditor.value = data.memory;
            }
            break;
        case "memories_list":
            if (data.memory != null) {
                renderMemories(data.memory);
                const memEd = document.getElementById("memory-editor");
                if (memEd) memEd.value = data.memory;
            }
            break;
        case "soul_only_set":
            document.getElementById("soul-only-toggle").checked = !!data.enabled;
            break;
        case "soul_ingested":
            flashStatus("soul-status", "Ingested -- LLM rebooting");
            break;
        case "llm_rebooted":
            document.getElementById("messages").innerHTML = "";
            messageCount = 0;
            currentSessionId = null;
            addSystemMessage("LLM rebooted");
            setBeacon("idle");
            break;
    }
}

function populateVoiceDropdown(voices, currentVoice) {
    const sel = document.getElementById("setting-voice");
    sel.innerHTML = "";
    if (voices && voices.length) {
        voices.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v;
            opt.textContent = v;
            sel.appendChild(opt);
        });
    } else {
        // Fallback: all voices
        ["Daniel (Enhanced)", "Daniel", "Samantha", "Samantha (Enhanced)", "Alex",
         "Ava (Premium)", "Ava (Enhanced)", "Allison (Enhanced)", "Karen",
         "Karen (Enhanced)", "Karen (Premium)", "Moira", "Tessa", "Tessa (Enhanced)",
         "Zarvox", "Fred", "Ralph", "Trinoids", "Whisper", "Albert",
         "Bad News", "Good News"].forEach(v => {
            const opt = document.createElement("option");
            opt.value = v;
            opt.textContent = v;
            sel.appendChild(opt);
        });
    }
    sel.value = currentVoice || "";
}

function initState(data) {
    // Avatar select
    const sel = document.getElementById("avatar-select");
    sel.innerHTML = "";
    (data.avatars || []).forEach((a) => {
        const opt = document.createElement("option");
        opt.value = a;
        opt.textContent = a;
        if (a === data.avatar) opt.selected = true;
        sel.appendChild(opt);
    });

    // Track avatar
    currentAvatar = data.avatar || "nexus";
    updateAvatarDisplay(currentAvatar);
    applyOutfitToMainSprite(userAvatar);

    // Status + stats
    document.getElementById("status-model").textContent = data.model || "--";
    const statusText = document.getElementById("status-text");
    if (statusText) { statusText.textContent = "online"; statusText.classList.add("online"); }
    {
        const ss = data.settings || {};
        const sv = document.getElementById("stat-voice");
        if (sv) sv.textContent = ss.voice || "--";
        const st2 = document.getElementById("stat-temp");
        if (st2) st2.textContent = ss.temperature ?? "--";
        const stk = document.getElementById("stat-tokens");
        if (stk) stk.textContent = ss.num_predict ?? "--";
        updateMsgCount();
    }

    // Soul & reminders editors
    document.getElementById("soul-editor").value = data.soul || "";
    document.getElementById("reminders-editor").value = data.reminders || "";
    document.getElementById("soul-only-toggle").checked = !!data.soul_only;

    // Memory
    if (data.memory != null) {
        renderMemories(data.memory);
        const memEditor = document.getElementById("memory-editor");
        if (memEditor) memEditor.value = data.memory;
    }

    // Settings
    const s = data.settings || {};
    document.getElementById("setting-host").value = s.ollama_host || "";
    document.getElementById("setting-model").value = s.ollama_model || "";
    populateVoiceDropdown(data.voices || [], s.voice || "Zarvox");
    document.getElementById("setting-greeting").value = s.greeting || "";
    // Populate audio device dropdown
    const audioDevSel = document.getElementById("setting-audio-device");
    audioDevSel.innerHTML = "";
    (data.audio_devices || [{id: "", name: "System Default"}]).forEach(d => {
        const opt = document.createElement("option");
        opt.value = d.id;
        opt.textContent = d.name;
        audioDevSel.appendChild(opt);
    });
    audioDevSel.value = s.audio_device || "";
    document.getElementById("setting-alert-sound").value = s.alert_sound || "Glass";
    setSlider("setting-rate", "rate-val", s.rate || 200);
    setSlider("setting-wake", "wake-val", s.wake_duration || 3);
    setSlider("setting-query", "query-val", s.query_duration || 5);
    setSlider("setting-followup", "followup-val", s.followup_duration || 5);
    setSlider("setting-temp", "temp-val", s.temperature ?? 0.7);
    setSlider("setting-topk", "topk-val", s.top_k ?? 40);
    setSlider("setting-topp", "topp-val", s.top_p ?? 0.9);
    setSlider("setting-maxtokens", "maxtok-val", s.num_predict ?? 300);
    setSlider("setting-repeat", "repeat-val", s.repeat_penalty ?? 1.1);

    // Tools
    renderTools(data.tools || []);

    // Skills
    send({ type: "list_skills" });

    // Scheduled tasks
    send({ type: "list_scheduled_tasks" });

    // Structured reminders
    renderReminders(data.reminders_json || []);

    // Sessions
    renderSessions(data.sessions || []);

    // History
    const messages = document.getElementById("messages");
    messages.innerHTML = "";
    const historyMsgs = data.history || [];
    messageCount = historyMsgs.length;
    historyMsgs.forEach((msg) => {
        addMessage(msg.role, msg.content);
    });

    // Wake word listener
    initWakeListener(data);
    if (wakeToggle.checked) {
        startWakeListener();
    }

    // Auto-start overlay if toggle was on from last session
    const ovToggle = document.getElementById("overlay-toggle-cb");
    if (ovToggle && ovToggle.checked) {
        send({ type: "start_overlay" });
    }
}

// ── Avatar display ──

// 16x16 pixel art SVGs for avatars
const AVATAR_SPRITES = {
    clippy: {
        // Paperclip with handlebar mustache, cigarette, and coffee
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 50" width="48" height="48">
            <!-- Paperclip wire body -->
            <path d="M18 42V14a8 8 0 0 1 16 0v22a5 5 0 0 1-10 0V16a2 2 0 0 1 4 0v18"
                  fill="none" stroke="#b0b0b0" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
            <!-- Left eye (round, bloodshot, half above head) -->
            <circle cx="22" cy="6" r="4.5" fill="#fff" stroke="#999" stroke-width="0.8"/>
            <line x1="18.2" y1="5" x2="19.8" y2="6" stroke="#cc3333" stroke-width="0.4" opacity="0.6"/>
            <line x1="18.8" y1="3.5" x2="20.3" y2="5" stroke="#cc3333" stroke-width="0.3" opacity="0.5"/>
            <line x1="19.2" y1="8.5" x2="20.2" y2="7.5" stroke="#cc3333" stroke-width="0.3" opacity="0.4"/>
            <circle class="clippy-pupil-l" cx="23" cy="5.8" r="1.8" fill="#222"/>
            <circle class="clippy-glint-l" cx="23.5" cy="5" r="0.6" fill="#fff"/>
            <!-- Right eye (round, bloodshot, half above head) -->
            <circle cx="30" cy="6" r="4.5" fill="#fff" stroke="#999" stroke-width="0.8"/>
            <line x1="33.8" y1="5" x2="32.2" y2="6" stroke="#cc3333" stroke-width="0.4" opacity="0.6"/>
            <line x1="33.2" y1="3.5" x2="31.7" y2="5" stroke="#cc3333" stroke-width="0.3" opacity="0.5"/>
            <line x1="32.8" y1="8.5" x2="31.8" y2="7.5" stroke="#cc3333" stroke-width="0.3" opacity="0.4"/>
            <circle class="clippy-pupil-r" cx="31" cy="5.8" r="1.8" fill="#222"/>
            <circle class="clippy-glint-r" cx="31.5" cy="5" r="0.6" fill="#fff"/>
            <!-- Brow lines -->
            <path d="M18 1.5 Q22 0.5 25 1.5" fill="none" stroke="#888" stroke-width="1" stroke-linecap="round"/>
            <path d="M27 1.5 Q30 0.5 34 1.5" fill="none" stroke="#888" stroke-width="1" stroke-linecap="round"/>
            <!-- Mouth -->
            <path d="M23 18 Q26 19.5 29 18" fill="none" stroke="#666" stroke-width="1.2" stroke-linecap="round"/>
            <!-- Sam Elliott walrus mustache -->
            <path d="M20 15 Q26 13.5 32 15" fill="none" stroke="#4a3218" stroke-width="2.5" stroke-linecap="round"/>
            <path d="M20.5 16 Q26 15 31.5 16" fill="none" stroke="#5a4228" stroke-width="2" stroke-linecap="round"/>
            <path d="M23 14.7 Q26 14 29 14.7" fill="none" stroke="#8a8078" stroke-width="1" stroke-linecap="round" opacity="0.6"/>
            <!-- Left droop -->
            <path d="M20 15 Q18 17 17 19.5" fill="none" stroke="#4a3218" stroke-width="2" stroke-linecap="round"/>
            <path d="M20.5 16 Q19 18 18 19.5" fill="none" stroke="#5a4228" stroke-width="1.5" stroke-linecap="round"/>
            <!-- Right droop -->
            <path d="M32 15 Q34 17 35 19.5" fill="none" stroke="#4a3218" stroke-width="2" stroke-linecap="round"/>
            <path d="M31.5 16 Q33 18 34 19.5" fill="none" stroke="#5a4228" stroke-width="1.5" stroke-linecap="round"/>
            <!-- Cigarette -->
            <g transform="rotate(12, 28, 17.5)">
                <rect x="28" y="16.5" width="3" height="2.2" rx="0.6" fill="#d4c89a"/>
                <rect x="31" y="16.5" width="10" height="2.2" rx="0.5" fill="#f5f0e0"/>
                <rect x="40" y="16.5" width="2.5" height="2.2" rx="0.4" fill="#999" opacity="0.7"/>
                <circle cx="43.5" cy="17.6" r="0.9" fill="#ff2200"/>
                <circle cx="43.5" cy="17.6" r="1.6" fill="#ff4400" opacity="0.2"/>
                <circle cx="43.6" cy="17.3" r="0.3" fill="#ff8800" opacity="0.8"/>
            </g>
            <!-- Smoke wisps -->
            <path d="M44 14 Q45 10 43 6 Q45 3 44 0" fill="none" stroke="#bbb" stroke-width="0.7" opacity="0.35"/>
            <path d="M45 13 Q47 9 45 5" fill="none" stroke="#aaa" stroke-width="0.5" opacity="0.25"/>
            <!-- Coffee mug -->
            <rect x="4" y="24" width="10" height="11" rx="2" fill="#8B4513" stroke="#6B3503" stroke-width="0.8"/>
            <path d="M14 27 Q17.5 27 17.5 30.5 Q17.5 34 14 34" fill="none" stroke="#6B3503" stroke-width="1.8" stroke-linecap="round"/>
            <text x="9" y="31" text-anchor="middle" font-family="monospace" font-size="3.2" fill="#d4a054" font-weight="bold">#1</text>
            <text x="9" y="34" text-anchor="middle" font-family="monospace" font-size="2" fill="#d4a054">FKN IT</text>
            <ellipse cx="9" cy="25.5" rx="4" ry="1.2" fill="#2a1505"/>
            <!-- Steam -->
            <path d="M7 22 Q8.5 20 7 18" fill="none" stroke="#ccc" stroke-width="0.8" opacity="0.4"/>
            <path d="M10 21 Q11.5 19 10 17" fill="none" stroke="#ccc" stroke-width="0.8" opacity="0.4"/>
        </svg>`,
    },
    nexus: {
        // Robot face icon - bold, reads clearly at small sizes
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
            <!-- Antenna -->
            <line x1="24" y1="2" x2="24" y2="8" stroke="#888" stroke-width="2.5" stroke-linecap="round"/>
            <circle cx="24" cy="2" r="2.5" fill="#00ff88"/>
            <!-- Head -->
            <rect x="8" y="8" width="32" height="28" rx="4" fill="#555" stroke="#666" stroke-width="1.5"/>
            <!-- Eye sockets -->
            <rect x="13" y="15" width="8" height="7" rx="1" fill="#222"/>
            <rect x="27" y="15" width="8" height="7" rx="1" fill="#222"/>
            <!-- Eye glow -->
            <rect x="15" y="17" width="4" height="3" rx="0.5" fill="#00ff88"/>
            <rect x="29" y="17" width="4" height="3" rx="0.5" fill="#00ff88"/>
            <!-- Mouth grille -->
            <rect x="16" y="28" width="16" height="4" rx="1" fill="#333"/>
            <line x1="20" y1="28" x2="20" y2="32" stroke="#555" stroke-width="1"/>
            <line x1="24" y1="28" x2="24" y2="32" stroke="#555" stroke-width="1"/>
            <line x1="28" y1="28" x2="28" y2="32" stroke="#555" stroke-width="1"/>
            <!-- Ear bolts -->
            <circle cx="8" cy="22" r="2.5" fill="#777" stroke="#888" stroke-width="1"/>
            <circle cx="40" cy="22" r="2.5" fill="#777" stroke="#888" stroke-width="1"/>
            <!-- Chest light -->
            <rect x="8" y="38" width="32" height="6" rx="2" fill="#444"/>
            <rect x="20" y="39.5" width="8" height="3" rx="1" fill="#ff2d55"/>
        </svg>`,
    },
    zelthor: {
        // Green alien with big eyes, antennae, and suit collar
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
            <!-- Antennae -->
            <line x1="18" y1="10" x2="14" y2="3" stroke="#2a9a3a" stroke-width="2" stroke-linecap="round"/>
            <circle cx="14" cy="2.5" r="2.5" fill="#00ff44"/>
            <circle cx="14.5" cy="2" r="0.8" fill="#88ff88" opacity="0.6"/>
            <line x1="30" y1="10" x2="34" y2="3" stroke="#2a9a3a" stroke-width="2" stroke-linecap="round"/>
            <circle cx="34" cy="2.5" r="2.5" fill="#00ff44"/>
            <circle cx="34.5" cy="2" r="0.8" fill="#88ff88" opacity="0.6"/>
            <!-- Head -->
            <ellipse cx="24" cy="22" rx="16" ry="18" fill="#3aaa4a" stroke="#2a8a3a" stroke-width="1.2"/>
            <ellipse cx="24" cy="23" rx="14" ry="16" fill="#44bb55"/>
            <!-- Forehead ridge -->
            <path d="M14 14 Q18 10 24 9 Q30 10 34 14" fill="none" stroke="#2a9a3a" stroke-width="1.5" stroke-linecap="round"/>
            <!-- Eyes -->
            <ellipse cx="17" cy="20" rx="5.5" ry="6.5" fill="#111" stroke="#1a6a2a" stroke-width="0.8" transform="rotate(-10, 17, 20)"/>
            <ellipse cx="31" cy="20" rx="5.5" ry="6.5" fill="#111" stroke="#1a6a2a" stroke-width="0.8" transform="rotate(10, 31, 20)"/>
            <ellipse cx="17" cy="20" rx="3" ry="3.5" fill="#00cc33"/>
            <ellipse cx="31" cy="20" rx="3" ry="3.5" fill="#00cc33"/>
            <ellipse cx="17.5" cy="19" rx="1" ry="1.2" fill="#88ff88" opacity="0.5"/>
            <ellipse cx="31.5" cy="19" rx="1" ry="1.2" fill="#88ff88" opacity="0.5"/>
            <!-- Nose -->
            <circle cx="22.5" cy="27" r="0.8" fill="#2a7a3a"/>
            <circle cx="25.5" cy="27" r="0.8" fill="#2a7a3a"/>
            <!-- Mouth -->
            <path d="M19 31 Q24 34 29 31" fill="none" stroke="#1a6a2a" stroke-width="1.2" stroke-linecap="round"/>
            <!-- Cheeks -->
            <path d="M8 24 L11 23 L11 25Z" fill="#2a9a3a" opacity="0.4"/>
            <path d="M40 24 L37 23 L37 25Z" fill="#2a9a3a" opacity="0.4"/>
            <!-- Collar -->
            <path d="M16 42 L20 39 L28 39 L32 42 L36 44 L12 44Z" fill="#225533" stroke="#1a4428" stroke-width="0.8"/>
            <circle cx="24" cy="42" r="1.5" fill="#00ff44"/>
        </svg>`,
    },
};

function updateAvatarDisplay(name) {
    const data = AVATAR_SPRITES[name] || AVATAR_SPRITES.nexus;
    const sprite = document.getElementById("avatar-sprite");
    sprite.innerHTML = data.svg;
    sprite.style.background = "transparent";
    // Avatar name shown in chip (no separate labels needed with top bar)
    // Update overlay toggle icon
    const overlayIcon = document.getElementById("overlay-icon-sprite");
    if (overlayIcon) {
        overlayIcon.innerHTML = data.svg.replace(/width="48"/g, 'width="18"').replace(/height="48"/g, 'height="18"');
    }
}

// ── Chat UI ──

function updateMsgCount() {
    const el = document.getElementById("stat-msgs");
    if (el) el.textContent = document.querySelectorAll("#messages .message").length;
}

function addMessage(role, content, name) {
    const container = document.getElementById("messages");
    const div = document.createElement("div");
    const isUser = role === "user";
    div.className = isUser ? "message user-msg" : "message";
    const avatarKey = isUser ? null : (name || currentAvatar || "nexus");
    const senderName = isUser ? "You" : avatarKey;
    const AVATAR_COLORS = { clippy: "#f0d000", nexus: "#bd00ff", zelthor: "#00ff88" };
    const color = isUser ? "#00d4ff" : (AVATAR_COLORS[avatarKey] || "#bd00ff");
    const initial = senderName.charAt(0).toUpperCase();
    const now = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    // User always gets person silhouette, agent gets its character sprite
    const spriteData = !isUser && AVATAR_SPRITES[avatarKey];
    let avatarInner;
    if (isUser) {
        avatarInner = `<div class="avatar chat-avatar-sprite"><svg viewBox="0 0 24 24" width="26" height="26">
            <defs><linearGradient id="uchat" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#7b2ff7"/><stop offset="100%" stop-color="#2d7fff"/></linearGradient></defs>
            <circle cx="12" cy="7" r="4" fill="url(#uchat)"/>
            <path d="M4 22 Q4 14 12 14 Q20 14 20 22Z" fill="url(#uchat)"/>
        </svg></div>`;
    } else if (spriteData) {
        avatarInner = `<div class="avatar chat-avatar-sprite">${spriteData.svg.replace(/width="48"/g, 'width="26"').replace(/height="48"/g, 'height="26"')}</div>`;
    } else {
        avatarInner = `<div class="avatar" style="background: ${color}">${initial}</div>`;
    }

    // Clippy gets the classic Windows yellow bubble
    const isClippy = !isUser && avatarKey === "clippy";
    if (isClippy) div.classList.add("clippy-msg");

    div.innerHTML = `
        ${avatarInner}
        <div class="body">
            <div class="header">
                <span class="sender" style="color: ${color}">${senderName}</span>
                <span class="timestamp">${now}</span>
            </div>
            <div class="content">${marked.parse(content || "")}</div>
        </div>
    `;

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;

    // Highlight code blocks
    div.querySelectorAll("pre code").forEach((el) => hljs.highlightElement(el));
    updateMsgCount();
}

function addSystemMessage(text) {
    const container = document.getElementById("messages");
    const div = document.createElement("div");
    div.className = "system-msg";
    div.textContent = `--- ${text} ---`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showThinking() {
    removeThinking();
    document.body.classList.add("thinking-active");
    const container = document.getElementById("messages");
    const div = document.createElement("div");
    div.className = "message thinking-msg";
    div.id = "thinking-indicator";
    const name = currentAvatar || "nexus";
    const thinkSprite = AVATAR_SPRITES[name];
    const thinkAvatar = thinkSprite
        ? `<div class="avatar chat-avatar-sprite">${thinkSprite.svg.replace(/width="48"/g, 'width="26"').replace(/height="48"/g, 'height="26"')}</div>`
        : `<div class="avatar" style="background: #bd00ff">${name.charAt(0).toUpperCase()}</div>`;
    div.innerHTML = `
        ${thinkAvatar}
        <div class="body">
            <div class="header">
                <span class="sender" style="color: #bd00ff">${name}</span>
                <div class="thinking">
                    <div class="thinking-dots"><span></span><span></span><span></span></div>
                </div>
            </div>
        </div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function removeThinking() {
    document.body.classList.remove("thinking-active");
    const el = document.getElementById("thinking-indicator");
    if (el) el.remove();
}

// ── Queued message indicator ──

function showQueuedIndicator() {
    // Remove any previous queued styling
    document.querySelectorAll('.message-queued').forEach(el => el.classList.remove('message-queued'));
    document.querySelectorAll('.queued-badge').forEach(el => el.remove());

    // Find the last user message and mark it as queued
    const messages = document.querySelectorAll('#messages .message');
    for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (msg.classList.contains('user-msg')) {
            msg.classList.add('message-queued');
            const badge = document.createElement('div');
            badge.className = 'queued-badge';
            badge.innerHTML = `
                <span class="queued-dot"></span>
                <span class="queued-text">QUEUED</span>
                <span class="queued-hint">send again to interrupt</span>
            `;
            const body = msg.querySelector('.body') || msg;
            body.appendChild(badge);
            const container = document.getElementById('messages');
            container.scrollTop = container.scrollHeight;
            // Auto-remove after 8s
            setTimeout(() => {
                msg.classList.remove('message-queued');
                badge.remove();
            }, 8000);
            break;
        }
    }
}

function removeQueuedIndicator() {
    document.querySelectorAll('.message-queued').forEach(el => el.classList.remove('message-queued'));
    document.querySelectorAll('.queued-badge').forEach(el => el.remove());
}

// ── Tools tab ──

function renderTools(toolNames) {
    const container = document.getElementById("tools-list");
    container.innerHTML = "";

    toolNames.forEach((name) => {
        const meta = TOOL_META[name] || { desc: "No description", params: [] };
        const div = document.createElement("div");
        div.className = "tool-card";

        let paramsHtml = "";
        if (meta.params.length) {
            paramsHtml = `<div class="tool-params">${meta.params.map((p) => `<span class="tool-param">${p}</span>`).join("")}</div>`;
        }

        div.innerHTML = `
            <div class="tool-name">${name}</div>
            <div class="tool-desc">${meta.desc}</div>
            ${paramsHtml}
        `;
        container.appendChild(div);
    });
}

// ── Status beacon ──

function setBeacon(state) {
    const el = document.getElementById("status-beacon");
    el.className = "beacon-" + state;
    el.title = state;
    // Sync avatar indicator
    const av = document.getElementById("avatar-beacon");
    if (av) {
        av.className = "avatar-beacon beacon-" + state;
    }
}

// ── State sounds (Web Audio API -- no files needed) ──

function playTone(freq, duration = 0.12, type = "sine", vol = 0.15) {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = type;
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(vol, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + duration);
    } catch (e) {}
}

// Wake word detected / start listening -- rising two-tone chirp
function playListenSound() { playTone(600, 0.08); setTimeout(() => playTone(900, 0.1), 90); }
// Done listening / thinking -- soft descending note
function playThinkSound() { playTone(700, 0.15, "triangle", 0.1); }
// Back to idle / exit conversation -- low soft blip
function playIdleSound() { playTone(400, 0.1, "sine", 0.08); setTimeout(() => playTone(300, 0.12, "sine", 0.06), 110); }

// ── Helpers ──

function flashStatus(id, text) {
    const el = document.getElementById(id);
    el.textContent = text;
    el.classList.add("visible");
    setTimeout(() => el.classList.remove("visible"), 2000);
}

// ── Tab switching (persisted) ──

function switchTab(tabName) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
    if (btn) btn.classList.add("active");
    const panel = document.querySelector(`[data-panel="${tabName}"]`);
    if (panel) panel.classList.add("active");
    localStorage.setItem("nexus-tab", tabName);
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// Restore last active tab
const savedTab = localStorage.getItem("nexus-tab");
if (savedTab) switchTab(savedTab);

// ── Send chat message ──

const input = document.getElementById("user-input");
const sendBtn = document.getElementById("send-btn");

let messageCount = 0;

// Persist voice toggle
const voiceToggleCb = document.getElementById("voice-toggle-cb");
voiceToggleCb.checked = localStorage.getItem("nexus-voice") === "true";

function sendMessage() {
    const text = input.value.trim();
    if (!text) return;

    messageCount++;
    const voiceOn = voiceToggleCb.checked;
    send({ type: "chat", content: text, voice: voiceOn });
    setBeacon("listening");
    input.value = "";
    input.style.height = "40px";

    // Flash send button
    sendBtn.classList.add('sent-flash');
    setTimeout(() => sendBtn.classList.remove('sent-flash'), 400);

    // Brief input bar glow
    const bar = document.getElementById('input-bar');
    if (bar) {
        bar.classList.add('sent-glow');
        setTimeout(() => bar.classList.remove('sent-glow'), 600);
    }
}

sendBtn.addEventListener("click", sendMessage);

input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Auto-resize textarea
input.addEventListener("input", () => {
    input.style.height = "40px";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
});

// ── Mic button (browser voice input via whisper-cli) ──

const micBtn = document.getElementById("mic-btn");
let mediaRecorder = null;
let micChunks = [];
let micRecording = false;
let micHoldTimer = null;
let micIsHold = false;

// Hold-to-talk: press and hold to record, release to send
micBtn.addEventListener("mousedown", (e) => {
    if (micBtn.classList.contains("transcribing")) return;
    micIsHold = false;
    micHoldTimer = setTimeout(() => {
        micIsHold = true;
        if (!micRecording) startMicRecording();
    }, 250);
});

micBtn.addEventListener("mouseup", () => {
    clearTimeout(micHoldTimer);
    if (micIsHold && micRecording) {
        stopMicRecording();
        micIsHold = false;
    }
});

micBtn.addEventListener("mouseleave", () => {
    clearTimeout(micHoldTimer);
    if (micIsHold && micRecording) {
        stopMicRecording();
        micIsHold = false;
    }
});

// Touch support for mobile hold-to-talk
micBtn.addEventListener("touchstart", (e) => {
    if (micBtn.classList.contains("transcribing")) return;
    e.preventDefault();
    micIsHold = false;
    micHoldTimer = setTimeout(() => {
        micIsHold = true;
        if (!micRecording) startMicRecording();
    }, 250);
});

micBtn.addEventListener("touchend", (e) => {
    e.preventDefault();
    clearTimeout(micHoldTimer);
    if (micIsHold && micRecording) {
        stopMicRecording();
        micIsHold = false;
        return;
    }
    // Short tap = toggle (same as click)
    if (!micIsHold && !micBtn.classList.contains("transcribing")) {
        if (micRecording) stopMicRecording();
        else startMicRecording();
    }
});

// Click fallback (short click = toggle)
micBtn.addEventListener("click", async () => {
    if (micBtn.classList.contains("transcribing") || micIsHold) return;

    if (micRecording) {
        stopMicRecording();
    } else {
        startMicRecording();
    }
});

async function startMicRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        micChunks = [];
        mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });

        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) micChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            if (micChunks.length === 0) return;

            micBtn.classList.remove("recording");
            micBtn.classList.add("transcribing");
            micRecording = false;

            const blob = new Blob(micChunks, { type: "audio/webm" });
            const form = new FormData();
            form.append("audio", blob, "recording.webm");

            try {
                const resp = await fetch("/transcribe", { method: "POST", body: form });
                const result = await resp.json();
                if (result.text && result.text.trim()) {
                    // Put transcribed text in input and auto-send
                    input.value = result.text.trim();
                    sendMessage();
                }
            } catch (err) {
                console.error("Transcription failed:", err);
            } finally {
                micBtn.classList.remove("transcribing");
            }
        };

        mediaRecorder.start();
        micRecording = true;
        micBtn.classList.add("recording");
    } catch (err) {
        console.error("Mic access denied:", err);
    }
}

function stopMicRecording() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
    }
    micRecording = false;
}

// ── Always-on wake word listener ──

let wakeListening = false;
let wakeStream = null;
let wakeRecorder = null;
let wakeAliases = [];
let wakeWord = "clippy";
let wakeChunkDuration = 3000; // 3s chunks
let wakeConversationMode = false;
let wakeSilenceCount = 0;
let wakeMuted = false; // true while clippy is speaking (prevent hearing himself)
let lastAssistantText = ""; // last thing clippy said, used to reject echo
const WAKE_MAX_SILENCE = 3;

// VAD (voice activity detection) -- skip whisper when no speech energy
let wakeAudioCtx = null;
let wakeAnalyser = null;
let wakeSpeechDetected = false;
const VAD_THRESHOLD = 2; // RMS energy threshold (0-128 scale, ~2 very sensitive, catches any sound above dead silence)

// Toggle stored in localStorage
const wakeToggle = document.getElementById("voice-toggle-cb");

function initWakeListener(data) {
    wakeWord = data.wake_word || "clippy";
    wakeAliases = (data.wake_aliases || [wakeWord]).map(a => a.toLowerCase());
}

async function startWakeListener() {
    if (wakeListening) return;
    try {
        wakeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        wakeListening = true;

        // Set up VAD analyser
        wakeAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const source = wakeAudioCtx.createMediaStreamSource(wakeStream);
        wakeAnalyser = wakeAudioCtx.createAnalyser();
        wakeAnalyser.fftSize = 512;
        source.connect(wakeAnalyser);

        console.log("[wake] Listening for wake word:", wakeWord);
        wakeLoop();
    } catch (err) {
        console.error("[wake] Mic access denied:", err);
    }
}

function stopWakeListener() {
    wakeListening = false;
    wakeConversationMode = false;
    if (wakeStream) {
        wakeStream.getTracks().forEach(t => t.stop());
        wakeStream = null;
    }
    if (wakeAudioCtx) {
        wakeAudioCtx.close();
        wakeAudioCtx = null;
        wakeAnalyser = null;
    }
    console.log("[wake] Stopped");
}

function wakeLoop() {
    if (!wakeListening || !wakeStream) return;
    // Don't record while the manual mic button is active or clippy is speaking
    if (micRecording || wakeMuted) {
        setTimeout(wakeLoop, 1000);
        return;
    }

    const chunks = [];
    const duration = wakeConversationMode ? 5000 : wakeChunkDuration;
    try {
        wakeRecorder = new MediaRecorder(wakeStream, { mimeType: "audio/webm;codecs=opus" });
    } catch (e) {
        // Stream might have been killed
        wakeListening = false;
        return;
    }

    wakeRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data);
    };

    // VAD: monitor energy during recording
    wakeSpeechDetected = false;
    let vadInterval = null;
    if (wakeAnalyser) {
        const vadBuf = new Uint8Array(wakeAnalyser.frequencyBinCount);
        vadInterval = setInterval(() => {
            wakeAnalyser.getByteTimeDomainData(vadBuf);
            let sum = 0;
            for (let i = 0; i < vadBuf.length; i++) {
                const v = vadBuf[i] - 128;
                sum += v * v;
            }
            const rms = Math.sqrt(sum / vadBuf.length);
            if (rms > VAD_THRESHOLD) wakeSpeechDetected = true;
        }, 100);
    }

    wakeRecorder.onstop = async () => {
        if (vadInterval) clearInterval(vadInterval);

        if (!wakeListening || chunks.length === 0) {
            setTimeout(wakeLoop, 100);
            return;
        }

        // Skip whisper if no speech energy detected (eliminates hallucinations on silence)
        if (!wakeSpeechDetected && !wakeConversationMode) {
            setTimeout(wakeLoop, 100);
            return;
        }

        const blob = new Blob(chunks, { type: "audio/webm" });
        const form = new FormData();
        form.append("audio", blob, "wake.webm");

        try {
            const resp = await fetch("/transcribe", { method: "POST", body: form });
            const result = await resp.json();
            const text = (result.text || "").trim();

            if (!text) {
                if (wakeConversationMode) {
                    wakeSilenceCount++;
                    console.log(`[wake] Silence (${wakeSilenceCount}/${WAKE_MAX_SILENCE})`);
                    if (wakeSilenceCount >= WAKE_MAX_SILENCE) {
                        wakeConversationMode = false;
                        setBeacon("idle");
                        playIdleSound();
                        console.log("[wake] Back to idle");
                    }
                }
                setTimeout(wakeLoop, 100);
                return;
            }

            // Echo rejection: only reject if transcription is a near-exact match of clippy's last words
            if (lastAssistantText && text.length > 5) {
                const heard = text.toLowerCase().trim();
                // Only reject if 80%+ of heard text is contained in what clippy said
                const overlapLen = Math.min(heard.length, lastAssistantText.length);
                if (overlapLen > 10 && lastAssistantText.includes(heard)) {
                    console.log("[wake] Echo rejected:", text);
                    setTimeout(wakeLoop, 100);
                    return;
                }
            }

            if (wakeConversationMode) {
                // In conversation -- send everything as a query
                wakeSilenceCount = 0;
                console.log("[wake] Follow-up:", text);
                send({ type: "chat", content: text, voice: true });
                setBeacon("listening");
                // Mute immediately -- wakeLoop resumes when mute timer clears
                wakeMuted = true;
                setTimeout(wakeLoop, 500);
                return;
            }

            // Check for wake word
            const lower = text.toLowerCase();
            let matched = false;
            let query = "";
            for (const alias of wakeAliases) {
                if (lower.includes(alias)) {
                    matched = true;
                    query = lower.replace(new RegExp(alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '[,\\.\\!\\? ]*', 'i'), '').trim();
                    break;
                }
            }

            if (!matched) {
                setTimeout(wakeLoop, 100);
                return;
            }

            console.log("[wake] Wake word detected!", text);
            wakeConversationMode = true;
            wakeSilenceCount = 0;
            playListenSound();

            if (query) {
                // Wake word + query in same utterance
                send({ type: "chat", content: query, voice: true });
                setBeacon("listening");
                wakeMuted = true;
                setTimeout(wakeLoop, 500);
            } else {
                // Just wake word -- listen for query
                console.log("[wake] Listening for query...");
                setBeacon("listening");
                setTimeout(wakeLoop, 300);
            }
        } catch (err) {
            console.error("[wake] Transcribe error:", err);
            setTimeout(wakeLoop, 1000);
        }
    };

    wakeRecorder.start();
    setTimeout(() => {
        if (wakeRecorder && wakeRecorder.state === "recording") {
            wakeRecorder.stop();
        }
    }, duration);
}

// Voice toggle controls wake word listener
wakeToggle.addEventListener("change", () => {
    localStorage.setItem("nexus-voice", wakeToggle.checked);
    if (wakeToggle.checked) {
        startWakeListener();
    } else {
        stopWakeListener();
    }
});

// ── Settings helpers ──

function setSlider(sliderId, labelId, value) {
    const slider = document.getElementById(sliderId);
    const label = document.getElementById(labelId);
    if (slider) slider.value = value;
    if (label) label.textContent = value;
}

// Wire up all sliders to show live values
["setting-rate:rate-val", "setting-temp:temp-val", "setting-topk:topk-val",
 "setting-topp:topp-val", "setting-maxtokens:maxtok-val", "setting-repeat:repeat-val",
 "setting-wake:wake-val", "setting-query:query-val", "setting-followup:followup-val"
].forEach(pair => {
    const [sid, lid] = pair.split(":");
    const slider = document.getElementById(sid);
    if (slider) {
        slider.addEventListener("input", () => {
            document.getElementById(lid).textContent = slider.value;
        });
    }
});

document.getElementById("save-settings-btn").addEventListener("click", () => {
    send({
        type: "save_settings",
        settings: {
            voice: document.getElementById("setting-voice").value,
            rate: parseInt(document.getElementById("setting-rate").value),
            greeting: document.getElementById("setting-greeting").value,
            ollama_host: document.getElementById("setting-host").value,
            ollama_model: document.getElementById("setting-model").value,
            temperature: parseFloat(document.getElementById("setting-temp").value),
            top_k: parseInt(document.getElementById("setting-topk").value),
            top_p: parseFloat(document.getElementById("setting-topp").value),
            num_predict: parseInt(document.getElementById("setting-maxtokens").value),
            repeat_penalty: parseFloat(document.getElementById("setting-repeat").value),
            wake_duration: parseInt(document.getElementById("setting-wake").value),
            query_duration: parseInt(document.getElementById("setting-query").value),
            followup_duration: parseInt(document.getElementById("setting-followup").value),
            audio_device: document.getElementById("setting-audio-device").value,
            alert_sound: document.getElementById("setting-alert-sound").value,
        },
    });
});

// ── Test voice button ──
document.getElementById("test-voice-btn").addEventListener("click", () => {
    send({ type: "test_voice", voice: document.getElementById("setting-voice").value });
});

// ── Save buttons ──

// Soul save is handled below with confirmation dialog

document.getElementById("save-reminders-btn").addEventListener("click", () => {
    send({ type: "save_reminders", content: document.getElementById("reminders-editor").value });
});

// ── Avatar switch ──

document.getElementById("avatar-select").addEventListener("change", (e) => {
    send({ type: "switch_avatar", avatar: e.target.value });
});

// ── Clear history ──

document.getElementById("clear-history-btn").addEventListener("click", () => {
    send({ type: "clear_history" });
});

document.getElementById("kill-llm-btn").addEventListener("click", () => {
    send({ type: "clear_history" });
    setBeacon("idle");
    removeThinking();
});

// ── Skills UI ──

// Detect current platform
const CURRENT_PLATFORM = (() => {
    const ua = navigator.userAgent.toLowerCase();
    if (ua.includes("mac")) return "macos";
    if (ua.includes("linux")) return "linux";
    if (ua.includes("win")) return "windows";
    return "cross-platform";
})();

let activeSkillset = CURRENT_PLATFORM;  // default to detected OS

function renderSkillsetTabs(skills) {
    const container = document.getElementById("skillset-tabs");
    container.innerHTML = "";

    // Collect unique platforms from skills
    const platforms = new Set(["macos", "cross-platform"]);
    skills.forEach((s) => platforms.add(s.platform || "macos"));

    const labels = { macos: "macOS", linux: "Linux", windows: "Windows", "cross-platform": "All" };
    const order = ["macos", "linux", "windows", "cross-platform"];

    order.forEach((p) => {
        if (!platforms.has(p)) return;
        const count = skills.filter((s) => (s.platform || "macos") === p).length;
        const btn = document.createElement("button");
        btn.className = "skillset-tab" + (activeSkillset === p ? " active" : "");
        btn.textContent = `${labels[p] || p} (${count})`;
        btn.addEventListener("click", () => {
            activeSkillset = p;
            renderSkillsetTabs(skills);
            renderSkillCards(skills);
        });
        container.appendChild(btn);
    });
}

function renderSkillCards(skills) {
    const container = document.getElementById("skills-list");
    container.innerHTML = "";

    const filtered = activeSkillset === "cross-platform"
        ? skills
        : skills.filter((s) => (s.platform || "macos") === activeSkillset || (s.platform || "") === "cross-platform");

    if (!filtered.length) {
        container.innerHTML = `<div class="skills-empty">No skills in this category. Create one or ask the agent.</div>`;
        return;
    }

    filtered.forEach((skill) => {
        const div = document.createElement("div");
        div.className = "skill-card";
        const platformBadge = skill.platform && skill.platform !== "macos"
            ? `<span class="skill-card-platform">${skill.platform}</span>` : "";
        div.innerHTML = `
            <div class="skill-card-header">
                <span class="skill-card-name">${skill.name}</span>
                <span class="skill-card-lang">${skill.language}</span>
                ${platformBadge}
            </div>
            <div class="skill-card-desc">${skill.description}</div>
        `;
        div.addEventListener("click", () => {
            send({ type: "get_skill", name: skill.name });
        });
        container.appendChild(div);
    });
}

function renderSkills(skills) {
    renderSkillsetTabs(skills);
    renderSkillCards(skills);
}

function openSkillEditor(skill) {
    const modal = document.getElementById("skill-editor-modal");
    modal.classList.remove("hidden");

    if (skill) {
        document.getElementById("skill-editor-title").textContent = "Edit Skill";
        document.getElementById("skill-name").value = skill.name || "";
        document.getElementById("skill-name").readOnly = true;
        document.getElementById("skill-desc").value = skill.description || "";
        document.getElementById("skill-lang").value = skill.language || "python";
        document.getElementById("skill-platform").value = skill.platform || CURRENT_PLATFORM;
        document.getElementById("skill-code").value = skill.code || "";
        document.getElementById("skill-delete-btn").style.display = "";
        document.getElementById("skill-run-btn").style.display = "";
    } else {
        document.getElementById("skill-editor-title").textContent = "New Skill";
        document.getElementById("skill-name").value = "";
        document.getElementById("skill-name").readOnly = false;
        document.getElementById("skill-desc").value = "";
        document.getElementById("skill-lang").value = "python";
        document.getElementById("skill-platform").value = CURRENT_PLATFORM;
        document.getElementById("skill-code").value = "";
        document.getElementById("skill-delete-btn").style.display = "none";
        document.getElementById("skill-run-btn").style.display = "none";
    }
}

function closeSkillEditor() {
    document.getElementById("skill-editor-modal").classList.add("hidden");
}

document.getElementById("new-skill-btn").addEventListener("click", () => {
    openSkillEditor(null);
});

document.getElementById("skill-editor-close").addEventListener("click", closeSkillEditor);

document.getElementById("skill-save-btn").addEventListener("click", () => {
    const name = document.getElementById("skill-name").value.trim();
    const description = document.getElementById("skill-desc").value.trim();
    const language = document.getElementById("skill-lang").value;
    const code = document.getElementById("skill-code").value;

    if (!name || !code) return;

    const platform = document.getElementById("skill-platform").value;
    send({
        type: "save_skill",
        name, description, language, code, platform,
    });
});

document.getElementById("skill-run-btn").addEventListener("click", () => {
    const name = document.getElementById("skill-name").value.trim();
    if (!name) return;
    send({ type: "run_skill", name });
});

document.getElementById("skill-delete-btn").addEventListener("click", () => {
    const name = document.getElementById("skill-name").value.trim();
    if (!name) return;
    if (confirm(`Delete skill "${name}"?`)) {
        send({ type: "delete_skill", name });
    }
});

// Soul save confirmation
const origSoulSave = document.getElementById("save-soul-btn");
origSoulSave.removeEventListener("click", origSoulSave._handler);
origSoulSave.addEventListener("click", () => {
    if (confirm("Overwrite the soul file? A backup will be saved.")) {
        send({ type: "save_soul", content: document.getElementById("soul-editor").value });
    }
});

// ── User Avatar Picker ──

// ── Per-avatar user icon variants (6 outfits each) ──
const AVATAR_USER_ICONS = {
    clippy: {
        default: { label: "Classic", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="15" cy="3.8" r="0.8" fill="#222"/>
        </svg>` },
        tophat: { label: "Top Hat", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="15" cy="3.8" r="0.8" fill="#222"/>
            <rect x="8.5" y="-5" width="7" height="4" rx="0.5" fill="#1a1a2e" stroke="#333" stroke-width="0.4"/>
            <rect x="7" y="-1.5" width="10" height="1.5" rx="0.5" fill="#1a1a2e"/>
        </svg>` },
        cowboy: { label: "Cowboy", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="15" cy="3.8" r="0.8" fill="#222"/>
            <ellipse cx="12" cy="-0.5" rx="10" ry="2" fill="#8B6914" stroke="#6b4c10" stroke-width="0.5"/>
            <path d="M7 -0.5 Q9 -3 12 -2.5 Q15 -3 17 -0.5" fill="#a07828" stroke="#8B6914" stroke-width="0.5"/>
        </svg>` },
        pirate: { label: "Pirate", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#1a1a1a" stroke="#333" stroke-width="0.5"/>
            <path d="M6 -1 Q8 -3 12 -2.5 Q16 -3 18 -1 L17 0 Q15 -1.5 12 -1.5 Q9 -1.5 7 0Z" fill="#1a1a2e"/>
        </svg>` },
        chef: { label: "Chef", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="15" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="9" cy="-3" r="2.5" fill="#f5f5f5" stroke="#ddd" stroke-width="0.4"/>
            <circle cx="15" cy="-3" r="2.5" fill="#f5f5f5" stroke="#ddd" stroke-width="0.4"/>
            <circle cx="12" cy="-4" r="2.8" fill="#f5f5f5" stroke="#ddd" stroke-width="0.4"/>
            <rect x="7" y="-1.5" width="10" height="1.5" rx="0.3" fill="#f5f5f5"/>
        </svg>` },
        punk: { label: "Punk", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <path d="M8 20V10a4 4 0 0 1 8 0v6a2.5 2.5 0 0 1-5 0v-5" fill="none" stroke="#b8b8b8" stroke-width="2" stroke-linecap="round"/>
            <circle cx="9.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="10" cy="3.8" r="0.8" fill="#222"/>
            <circle cx="14.5" cy="4" r="2.2" fill="#fff" stroke="#888" stroke-width="0.5"/><circle cx="15" cy="3.8" r="0.8" fill="#222"/>
            <path d="M12 0 L12 -4.5" stroke="#ff2d55" stroke-width="2.5" stroke-linecap="round"/>
            <path d="M10 0 L9 -4" stroke="#ff2d55" stroke-width="1.5" stroke-linecap="round"/>
            <path d="M14 0 L15 -4" stroke="#ff2d55" stroke-width="1.5" stroke-linecap="round"/>
        </svg>` },
    },
    nexus: {
        default: { label: "Standard", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#606878" stroke="#4a5060" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#8890a0" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#ff2d55"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#0af"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#0af"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#3a3e48"/>
        </svg>` },
        military: { label: "Military", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#4a5a3a" stroke="#3a4a2a" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#8890a0" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#ff2d55"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff4400"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff4400"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#2a3a1a"/>
            <rect x="5" y="4" width="14" height="3" rx="1" fill="#4a5a3a" stroke="#3a4a2a" stroke-width="0.4"/>
        </svg>` },
        gold: { label: "Gold Plated", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#c8a83e" stroke="#a08828" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#d4b44a" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#ffe066"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#fff" opacity="0.8"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#fff" opacity="0.8"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#a08020"/>
        </svg>` },
        neon: { label: "Neon", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#1a1a2e" stroke="#00ff88" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#00ff88" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#00ff88"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff00ff"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff00ff"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#0a0a1a"/>
        </svg>` },
        rusty: { label: "Rusty", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#8a5a3a" stroke="#6a4020" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#7a6050" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#cc4422"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#44aa88" opacity="0.7"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#44aa88" opacity="0.7"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#5a3a20"/>
        </svg>` },
        stealth: { label: "Stealth", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <rect x="4" y="6" width="16" height="13" rx="2.5" fill="#1a1a1a" stroke="#333" stroke-width="0.7"/>
            <line x1="12" y1="2.5" x2="12" y2="6" stroke="#333" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="12" cy="2" r="1.8" fill="#333"/>
            <rect x="7" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff2d55" opacity="0.8"/>
            <rect x="13.5" y="10" width="3.5" height="2.5" rx="0.8" fill="#ff2d55" opacity="0.8"/>
            <rect x="8" y="15" width="8" height="2.5" rx="1" fill="#111"/>
        </svg>` },
    },
    zelthor: {
        default: { label: "Classic", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <path d="M6 6 Q8 3 12 3 Q16 3 18 6" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <path d="M10 17 Q12 18.5 14 17" fill="none" stroke="#2a7a3a" stroke-width="0.8" stroke-linecap="round"/>
        </svg>` },
        commander: { label: "Commander", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <path d="M4 5 L8 3 L12 1.5 L16 3 L20 5 L18 6 L6 6Z" fill="#2a2a5a" stroke="#4444aa" stroke-width="0.4"/>
            <circle cx="12" cy="4" r="0.8" fill="#ffcc00"/>
        </svg>` },
        spacesuit: { label: "Spacesuit", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="13" rx="9" ry="9.5" fill="none" stroke="#aab" stroke-width="1.5"/>
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
        </svg>` },
        tribal: { label: "Tribal", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <path d="M5 12 L4 11 L5 10" stroke="#cc4400" stroke-width="0.8" fill="none"/>
            <path d="M19 12 L20 11 L19 10" stroke="#cc4400" stroke-width="0.8" fill="none"/>
            <path d="M10 7.5 L12 6 L14 7.5" stroke="#cc4400" stroke-width="0.8" fill="none"/>
        </svg>` },
        hacker: { label: "Hacker", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00ff00"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00ff00"/>
            <rect x="5" y="3" width="14" height="4" rx="1" fill="#1a1a2e" stroke="#333" stroke-width="0.4"/>
            <rect x="7" y="4" width="2" height="0.6" rx="0.2" fill="#00ff00" opacity="0.7"/>
            <rect x="10" y="4" width="3" height="0.6" rx="0.2" fill="#00ff00" opacity="0.5"/>
        </svg>` },
        royal: { label: "Royal", svg: `<svg viewBox="0 0 24 24" width="28" height="28">
            <ellipse cx="12" cy="14" rx="7.5" ry="8.5" fill="#44bb55" stroke="#2a8a3a" stroke-width="0.6"/>
            <ellipse cx="8.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="15.5" cy="11.5" rx="3.2" ry="2" fill="#111"/>
            <ellipse cx="8.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <ellipse cx="15.5" cy="11.5" rx="1.6" ry="1" fill="#00dd44"/>
            <path d="M5 5 L7 1 L9.5 4 L12 0.5 L14.5 4 L17 1 L19 5Z" fill="#cc8800" stroke="#aa6600" stroke-width="0.4"/>
            <circle cx="12" cy="1.5" r="0.8" fill="#ff2244"/>
        </svg>` },
    },
};

function getUserAvatarsForCurrentAgent() {
    return AVATAR_USER_ICONS[currentAvatar] || AVATAR_USER_ICONS.nexus;
}

// Accessory overlays for the main agent sprite (56x56 viewBox, eyes at y=13)
const CLIPPY_ACCESSORY_OVERLAYS = {
    default: "",
    tophat: `<rect x="17" y="-5" width="18" height="9" rx="1" fill="#1a1a2e" stroke="#333" stroke-width="0.6"/>
             <rect x="14" y="3" width="24" height="3" rx="1" fill="#1a1a2e"/>`,
    cowboy: `<ellipse cx="26" cy="3" rx="22" ry="4.5" fill="#8B6914" stroke="#6b4c10" stroke-width="0.7"/>
             <path d="M15 3 Q19 -4 26 -3 Q33 -4 37 3" fill="#a07828" stroke="#8B6914" stroke-width="0.7"/>`,
    pirate: `<circle cx="30" cy="6" r="3.8" fill="#1a1a1a" stroke="#333" stroke-width="0.8"/>
             <path d="M27 4 L34 3" stroke="#333" stroke-width="0.8"/>
             <path d="M27 8 L34 9" stroke="#333" stroke-width="0.8"/>
             <path d="M13 2 Q17 -2 26 -1 Q35 -2 39 2 L37 4 Q33 1 26 1 Q19 1 15 4Z" fill="#1a1a2e"/>`,
    chef: `<circle cx="20" cy="-2" r="5.5" fill="#f5f5f5" stroke="#ddd" stroke-width="0.5"/>
            <circle cx="32" cy="-2" r="5.5" fill="#f5f5f5" stroke="#ddd" stroke-width="0.5"/>
            <circle cx="26" cy="-4" r="6" fill="#f5f5f5" stroke="#ddd" stroke-width="0.5"/>
            <rect x="15" y="1" width="22" height="4" rx="0.5" fill="#f5f5f5" stroke="#ddd" stroke-width="0.4"/>`,
    punk: `<path d="M26 4 L26 -5" stroke="#ff2d55" stroke-width="5.5" stroke-linecap="round"/>
            <path d="M21 4 L19 -4" stroke="#ff2d55" stroke-width="3.5" stroke-linecap="round"/>
            <path d="M31 4 L33 -4" stroke="#ff2d55" stroke-width="3.5" stroke-linecap="round"/>`,
};

function applyOutfitToMainSprite(outfitKey) {
    const sprite = document.getElementById("avatar-sprite");
    if (currentAvatar === "clippy") {
        const baseSvg = AVATAR_SPRITES.clippy.svg;
        const overlay = CLIPPY_ACCESSORY_OVERLAYS[outfitKey] || "";
        if (overlay) {
            sprite.innerHTML = baseSvg.replace("</svg>", overlay + "</svg>");
        } else {
            sprite.innerHTML = baseSvg;
        }
    } else {
        // For nexus/zelthor: outfit picker icons ARE the full sprite, just use them
        const icons = AVATAR_USER_ICONS[currentAvatar];
        if (icons && icons[outfitKey]) {
            // Scale the outfit SVG to sidebar sprite size
            let svg = icons[outfitKey].svg;
            svg = svg.replace(/width="28"/, 'width="48"').replace(/height="28"/, 'height="48"');
            sprite.innerHTML = svg;
        } else {
            sprite.innerHTML = (AVATAR_SPRITES[currentAvatar] || AVATAR_SPRITES.nexus).svg;
        }
    }
}

let userAvatar = localStorage.getItem("nexus-user-avatar") || "default";

function renderUserAvatarPicker() {
    const container = document.getElementById("user-avatar-picker");
    container.innerHTML = "";
    const icons = getUserAvatarsForCurrentAgent();
    // If current selection doesn't exist in new avatar set, reset to default
    if (!icons[userAvatar]) {
        userAvatar = "default";
        localStorage.setItem("nexus-user-avatar", userAvatar);
    }
    Object.entries(icons).forEach(([key, av]) => {
        const btn = document.createElement("button");
        btn.className = "user-avatar-btn" + (key === userAvatar ? " active" : "");
        btn.title = av.label;
        btn.innerHTML = av.svg;
        btn.addEventListener("click", () => {
            userAvatar = key;
            localStorage.setItem("nexus-user-avatar", key);
            renderUserAvatarPicker();
            applyOutfitToMainSprite(key);
        });
        container.appendChild(btn);
    });
}

renderUserAvatarPicker();
applyOutfitToMainSprite(userAvatar);

// ── Sessions UI ──

let currentSessionId = null;

function _buildSessionRows(container, sessions) {
    container.innerHTML = "";
    if (!sessions || !sessions.length) {
        container.innerHTML = `<div class="sessions-empty">no saved sessions</div>`;
        return;
    }
    sessions.forEach((s) => {
        const row = document.createElement("div");
        row.className = "session-row" + (s.id === currentSessionId ? " active" : "");

        const date = new Date(s.created * 1000);
        const dateStr = date.toLocaleDateString([], { month: "short", day: "numeric" });

        row.innerHTML = `
            <span class="session-prompt">&gt;</span>
            <input class="session-name" value="${s.name.replace(/"/g, '&quot;')}" spellcheck="false" readonly>
            <span class="session-meta">${dateStr} (${s.message_count})</span>
            <button class="session-delete" title="Delete">&times;</button>
        `;

        const nameInput = row.querySelector(".session-name");

        row.addEventListener("click", (e) => {
            if (e.target === nameInput && !nameInput.readOnly) return;
            if (e.target.classList.contains("session-delete")) return;
            currentSessionId = s.id;
            send({ type: "load_session", id: s.id });
        });

        nameInput.addEventListener("dblclick", (e) => {
            e.stopPropagation();
            nameInput.readOnly = false;
            nameInput.focus();
            nameInput.select();
        });

        const saveName = () => {
            nameInput.readOnly = true;
            const newName = nameInput.value.trim();
            if (newName && newName !== s.name) {
                send({ type: "rename_session", id: s.id, name: newName });
            }
        };
        nameInput.addEventListener("blur", saveName);
        nameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); nameInput.blur(); }
            if (e.key === "Escape") { nameInput.value = s.name; nameInput.blur(); }
        });

        row.querySelector(".session-delete").addEventListener("click", (e) => {
            e.stopPropagation();
            send({ type: "delete_session", id: s.id });
            if (s.id === currentSessionId) currentSessionId = null;
        });

        container.appendChild(row);
    });
}

function renderSessions(sessions) {
    // Desktop sidebar
    _buildSessionRows(document.getElementById("sessions-list"), sessions);
    // Mobile panel
    const mobileList = document.getElementById("mobile-sessions-list");
    if (mobileList) _buildSessionRows(mobileList, sessions);
}

// Mobile sessions collapse toggle
(() => {
    const toggle = document.getElementById("mobile-sessions-toggle");
    const list = document.getElementById("mobile-sessions-list");
    const header = document.querySelector(".mobile-sessions-header");
    if (!toggle || !list || !header) return;
    // Start collapsed
    list.classList.add("collapsed");
    toggle.classList.add("collapsed");
    header.addEventListener("click", () => {
        list.classList.toggle("collapsed");
        toggle.classList.toggle("collapsed");
    });
})();

document.getElementById("new-session-btn").addEventListener("click", () => {
    currentSessionId = null;
    send({ type: "new_session" });
});

// Sessions are now always visible in the sidebar — no toggle needed.

// ── Reminders UI ──

const DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

// Day picker toggles
document.querySelectorAll(".day-btn").forEach((btn) => {
    btn.addEventListener("click", () => btn.classList.toggle("active"));
});

// Trigger type switching
document.getElementById("reminder-trigger-type").addEventListener("change", (e) => {
    const isTime = e.target.value === "time";
    const isKeyword = e.target.value === "keyword";
    document.getElementById("reminder-time-opts").classList.toggle("hidden", !isTime);
    document.getElementById("reminder-keyword-opts").classList.toggle("hidden", !isKeyword);
});

// Track current reminders for duplicate detection
let currentReminders = [];

// Add reminder
document.getElementById("add-reminder-btn").addEventListener("click", () => {
    const content = document.getElementById("reminder-content").value.trim();
    if (!content) return;

    // Check for duplicate
    const isDuplicate = currentReminders.some(r =>
        r.content.toLowerCase() === content.toLowerCase()
    );
    if (isDuplicate) {
        flashStatus("reminders-status", "Already exists!");
        return;
    }

    const triggerType = document.getElementById("reminder-trigger-type").value;
    const reminder = { type: "add_reminder", content, trigger_type: triggerType };

    if (triggerType === "time") {
        reminder.time = document.getElementById("reminder-time").value;
        reminder.days = Array.from(document.querySelectorAll(".day-btn.active")).map((b) => b.dataset.day);
        const expires = document.getElementById("reminder-expires").value;
        if (expires) reminder.expires = expires;
    } else if (triggerType === "keyword") {
        reminder.keyword = document.getElementById("reminder-keyword").value.trim();
    }

    send(reminder);
    document.getElementById("reminder-content").value = "";
    flashStatus("reminders-status", "Added");
});

function renderReminders(reminders) {
    currentReminders = reminders || [];
    const container = document.getElementById("reminders-list");
    container.innerHTML = "";

    if (!reminders || !reminders.length) {
        container.innerHTML = `<div class="reminders-empty">No active reminders</div>`;
        return;
    }

    reminders.forEach((r) => {
        const div = document.createElement("div");
        div.className = "reminder-card";

        let schedule = "";
        if (r.trigger_type === "time") {
            const days = (r.days || []).map((d) => DAY_LABELS[d] || d).join(", ");
            schedule = `${r.time || "09:00"} ${days || "daily"}`;
            if (r.expires) schedule += ` (until ${r.expires})`;
        } else if (r.trigger_type === "startup") {
            schedule = "on startup";
        } else {
            schedule = `keyword: ${r.keyword || "always"}`;
        }

        div.innerHTML = `
            <div class="reminder-card-body">
                <div class="reminder-card-content">${r.content}</div>
                <div class="reminder-card-schedule">${schedule}</div>
            </div>
            <button class="reminder-card-delete" title="Delete">&times;</button>
        `;

        div.querySelector(".reminder-card-delete").addEventListener("click", () => {
            send({ type: "delete_reminder", id: r.id });
        });

        container.appendChild(div);
    });
}

// ── Schedule UI ──

function renderScheduledTasks(tasks) {
    const container = document.getElementById("schedule-list");
    container.innerHTML = "";

    if (!tasks || !tasks.length) {
        container.innerHTML = `<div class="schedule-empty">No scheduled tasks</div>`;
        return;
    }

    tasks.forEach((t) => {
        const div = document.createElement("div");
        div.className = "schedule-card";
        const whenLabel = t.repeat ? "Repeating" : "One-time";
        div.innerHTML = `
            <div class="schedule-card-body">
                <div class="schedule-card-name">${t.name}</div>
                <div class="schedule-card-command">${t.command || ""}</div>
                <div class="schedule-card-when"><span class="schedule-badge ${t.repeat ? 'badge-repeat' : 'badge-once'}">${whenLabel}</span></div>
            </div>
            <button class="schedule-card-delete" title="Remove">&times;</button>
        `;
        div.querySelector(".schedule-card-delete").addEventListener("click", () => {
            send({ type: "remove_scheduled_task", name: t.name });
        });
        container.appendChild(div);
    });
}

// Schedule frequency switching
document.getElementById("schedule-frequency").addEventListener("change", (e) => {
    const freq = e.target.value;
    document.getElementById("schedule-time-group").classList.toggle("hidden", freq === "custom");
    document.getElementById("schedule-date-group").classList.toggle("hidden", freq !== "once");
    document.getElementById("schedule-day-group").classList.toggle("hidden", freq !== "weekly");
    document.getElementById("schedule-cron-group").classList.toggle("hidden", freq !== "custom");
});

document.getElementById("add-schedule-btn").addEventListener("click", () => {
    const name = document.getElementById("schedule-name").value.trim();
    const command = document.getElementById("schedule-command").value.trim();
    const freq = document.getElementById("schedule-frequency").value;
    const timeVal = document.getElementById("schedule-time").value || "09:00";
    const [hour, minute] = timeVal.split(":").map(Number);

    if (!name || !command) return;

    let when, repeat;

    if (freq === "once") {
        const dateVal = document.getElementById("schedule-date").value;
        if (!dateVal) { flashStatus("schedule-status", "Pick a date"); return; }
        when = `${dateVal} ${timeVal}`;
        repeat = false;
    } else if (freq === "daily") {
        when = `${minute} ${hour} * * *`;
        repeat = true;
    } else if (freq === "weekdays") {
        when = `${minute} ${hour} * * 1-5`;
        repeat = true;
    } else if (freq === "weekly") {
        const day = document.getElementById("schedule-day").value;
        when = `${minute} ${hour} * * ${day}`;
        repeat = true;
    } else if (freq === "hourly") {
        when = `${minute} * * * *`;
        repeat = true;
    } else if (freq === "custom") {
        when = document.getElementById("schedule-cron").value.trim();
        if (!when) { flashStatus("schedule-status", "Enter cron expression"); return; }
        repeat = true;
    }

    send({ type: "add_scheduled_task", name, command, when, repeat });
    document.getElementById("schedule-name").value = "";
    document.getElementById("schedule-command").value = "";
});

// ── Memories UI ──

function renderMemories(memoryText) {
    const container = document.getElementById("memories-list");
    if (!container) return;
    container.innerHTML = "";

    if (!memoryText || !memoryText.trim()) {
        container.innerHTML = `<div class="memories-empty">No memories stored yet</div>`;
        return;
    }

    const lines = memoryText.split("\n");
    let currentSection = "";

    lines.forEach(line => {
        if (line.startsWith("## ")) {
            currentSection = line.replace("## ", "").trim();
            const header = document.createElement("div");
            header.className = "memory-section-header";
            header.textContent = currentSection;
            container.appendChild(header);
        } else if (line.startsWith("- ")) {
            const text = line.substring(2).trim();
            if (!text) return;
            const card = document.createElement("div");
            card.className = "memory-card";
            card.innerHTML = `
                <div class="memory-card-text">${text}</div>
                <button class="memory-card-delete" title="Delete">&times;</button>
            `;
            card.querySelector(".memory-card-delete").addEventListener("click", () => {
                send({ type: "delete_memory", text: text });
            });
            container.appendChild(card);
        }
    });
}

document.getElementById("add-memory-btn").addEventListener("click", () => {
    const content = document.getElementById("memory-content").value.trim();
    if (!content) return;
    const category = document.getElementById("memory-category").value;
    send({ type: "add_memory", category, content });
    document.getElementById("memory-content").value = "";
    flashStatus("memory-status", "Added");
});

document.getElementById("save-memory-raw-btn").addEventListener("click", () => {
    send({ type: "save_memory_raw", content: document.getElementById("memory-editor").value });
});

document.getElementById("ingest-memory-btn").addEventListener("click", () => {
    if (confirm("Reload memories and reboot the LLM? Current chat will be cleared.")) {
        send({ type: "reboot_llm" });
        setBeacon("idle");
        removeThinking();
    }
});

// ── Soul Only toggle + Ingest Soul ──

document.getElementById("soul-only-toggle").addEventListener("change", (e) => {
    send({ type: "set_soul_only", enabled: e.target.checked });
});

document.getElementById("ingest-soul-btn").addEventListener("click", () => {
    if (confirm("Save soul and resurrect the LLM with it? Current chat will be cleared.")) {
        send({ type: "ingest_soul", content: document.getElementById("soul-editor").value });
    }
});

// ── Reboot LLM ──

document.getElementById("reboot-llm-btn").addEventListener("click", () => {
    if (confirm("Kill the LLM and reboot fresh? Current chat will be cleared.")) {
        send({ type: "reboot_llm" });
        setBeacon("idle");
        removeThinking();
    }
});

// ── Overlay toggle ──

const overlayToggle = document.getElementById("overlay-toggle-cb");
overlayToggle.checked = localStorage.getItem("nexus-overlay") === "true";
overlayToggle.addEventListener("change", () => {
    localStorage.setItem("nexus-overlay", overlayToggle.checked);
    send({ type: overlayToggle.checked ? "start_overlay" : "stop_overlay" });
});

// ── Init ──

connect();

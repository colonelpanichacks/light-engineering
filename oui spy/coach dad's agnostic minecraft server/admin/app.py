"""
Coach Dad's Minecraft Admin Panel
God-mode control panel for your server via RCON.
"""

import os
import json
import re
import random
import time
import uuid
import threading
import requests
from functools import wraps
from collections import deque
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
# mcrcon not used directly — we have a thread-safe raw RCON implementation below

app = Flask(__name__)
app.secret_key = os.environ.get("ADMIN_SECRET_KEY", "coach-dad-secret-change-me")

# ---------------------------------------------------------------------------
# RCON Connection
# ---------------------------------------------------------------------------
RCON_HOST = os.environ.get("RCON_HOST", "mc")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "changeme")
ADMIN_USER = os.environ.get("ADMIN_USER", "coach_dad")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")


import struct
import socket

_rcon_lock = threading.Lock()
_mlx_lock = threading.Lock()  # Serialize MLX requests to prevent concurrent GPU OOM


def _rcon_packet(req_id, ptype, payload):
    """Build an RCON packet: length (4) + request_id (4) + type (4) + payload + \0\0"""
    data = struct.pack('<ii', req_id, ptype) + payload.encode('utf-8') + b'\x00\x00'
    return struct.pack('<i', len(data)) + data


def _rcon_recv(sock):
    """Read a full RCON response packet from the socket."""
    raw_len = b''
    while len(raw_len) < 4:
        chunk = sock.recv(4 - len(raw_len))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        raw_len += chunk
    length = struct.unpack('<i', raw_len)[0]
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        data += chunk
    req_id = struct.unpack('<i', data[0:4])[0]
    ptype = struct.unpack('<i', data[4:8])[0]
    payload = data[8:-2].decode('utf-8', errors='replace')
    return req_id, ptype, payload


def _rcon_connect():
    """Open an authenticated RCON connection (thread-safe, no signals)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((RCON_HOST, RCON_PORT))
    # Authenticate (type 3 = login)
    sock.sendall(_rcon_packet(1, 3, RCON_PASSWORD))
    rid, rtype, _ = _rcon_recv(sock)
    if rid == -1:
        sock.close()
        raise PermissionError("RCON authentication failed")
    return sock


def _rcon_execute(sock, command):
    """Send a command on an open RCON socket and return the response."""
    sock.sendall(_rcon_packet(2, 2, command))
    _, _, payload = _rcon_recv(sock)
    return payload.strip()


def rcon_cmd(command):
    """Send a single RCON command and return the response (thread-safe)."""
    try:
        with _rcon_lock:
            sock = _rcon_connect()
            try:
                resp = _rcon_execute(sock, command)
            finally:
                sock.close()
            return resp
    except Exception as e:
        return f"[RCON ERROR] {str(e)}"


def rcon_multi(commands):
    """Send multiple RCON commands in one connection, return all responses (thread-safe)."""
    results = []
    try:
        with _rcon_lock:
            sock = _rcon_connect()
            try:
                for cmd_str in commands:
                    resp = _rcon_execute(sock, cmd_str)
                    results.append(resp)
                    time.sleep(0.05)
            finally:
                sock.close()
    except Exception as e:
        results.append(f"[RCON ERROR] {str(e)}")
    return results


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == ADMIN_USER and pwd == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong credentials")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API — Server Info
# ---------------------------------------------------------------------------
@app.route("/api/status")
@login_required
def api_status():
    player_list = rcon_cmd("list")
    # Parse "There are X of Y players online: name1, name2"
    players = []
    online = 0
    max_players = 20
    try:
        if ":" in player_list:
            parts = player_list.split(":")
            names = parts[1].strip()
            if names:
                players = [n.strip() for n in names.split(",") if n.strip()]
            nums = parts[0]
            numbers = re.findall(r'\d+', nums)
            if len(numbers) >= 2:
                online = int(numbers[0])
                max_players = int(numbers[1])
    except Exception:
        pass

    difficulty = rcon_cmd("difficulty")
    return jsonify({
        "player_list": player_list,
        "players": players,
        "online": online,
        "max_players": max_players,
        "difficulty": difficulty,
    })


@app.route("/api/player_positions")
@login_required
def api_player_positions():
    """Get every online player's XYZ position + dimension."""
    player_list = rcon_cmd("list")
    players = []
    try:
        if ":" in player_list:
            names = player_list.split(":")[1].strip()
            if names:
                players = [n.strip() for n in names.split(",") if n.strip()]
    except Exception:
        pass

    if not players:
        return jsonify({"players": []})

    results = []
    try:
        cmds_per_player = []
        for p in players:
            cmds_per_player.append([
                f"data get entity {p} Pos",
                f"data get entity {p} Dimension",
                f"data get entity {p} Health",
                f"data get entity {p} foodLevel",
                f"data get entity {p} XpLevel",
                f"data get entity {p} playerGameType",
            ])

        with _rcon_lock:
            sock = _rcon_connect()
            try:
                for pi, p in enumerate(players):
                    resps = []
                    for c in cmds_per_player[pi]:
                        resps.append(_rcon_execute(sock, c))
                    time.sleep(0.02)

                    pos_resp, dim_resp, health_resp, food_resp, xp_resp, gm_resp = resps

                    entry = {"name": p, "x": 0, "y": 0, "z": 0,
                             "dimension": "overworld", "health": 20,
                             "food": 20, "xp_level": 0, "gamemode": 0}

                    pos_match = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', pos_resp or "")
                    if pos_match:
                        entry["x"] = round(float(pos_match.group(1)), 1)
                        entry["y"] = round(float(pos_match.group(2)), 1)
                        entry["z"] = round(float(pos_match.group(3)), 1)

                    dim_match = re.search(r'"minecraft:(\w+)"', dim_resp or "")
                    if dim_match:
                        entry["dimension"] = dim_match.group(1)

                    health_match = re.search(r':\s*(-?[\d.]+)f?$', (health_resp or "").strip())
                    if health_match:
                        entry["health"] = round(float(health_match.group(1)), 1)

                    food_match = re.search(r':\s*(\d+)$', (food_resp or "").strip())
                    if food_match:
                        entry["food"] = int(food_match.group(1))

                    xp_match = re.search(r':\s*(\d+)$', (xp_resp or "").strip())
                    if xp_match:
                        entry["xp_level"] = int(xp_match.group(1))

                    gm_match = re.search(r':\s*(\d+)$', (gm_resp or "").strip())
                    if gm_match:
                        entry["gamemode"] = int(gm_match.group(1))

                    results.append(entry)
            finally:
                sock.close()
    except Exception as e:
        return jsonify({"players": [], "error": str(e)})

    return jsonify({"players": results})


@app.route("/api/server_info")
@login_required
def api_server_info():
    """Extended server metrics for the admin view."""
    cmds = ["seed", "tps", "mspt", "whitelist list", "banlist"]
    resps = rcon_multi(cmds)
    keys = ["seed", "tps", "mspt", "whitelist", "banlist"]
    result = {}
    for i, k in enumerate(keys):
        result[k] = resps[i] if i < len(resps) else ""
    return jsonify(result)


@app.route("/api/command", methods=["POST"])
@login_required
def api_command():
    """Run a raw RCON command."""
    cmd = request.json.get("command", "")
    if not cmd:
        return jsonify({"error": "No command provided"}), 400
    result = rcon_cmd(cmd)
    return jsonify({"command": cmd, "result": result})


@app.route("/api/multi_command", methods=["POST"])
@login_required
def api_multi_command():
    """Run multiple RCON commands sequentially."""
    cmds = request.json.get("commands", [])
    if not cmds:
        return jsonify({"error": "No commands provided"}), 400
    results = rcon_multi(cmds)
    return jsonify({"commands": cmds, "results": results})


# ---------------------------------------------------------------------------
# API — Game Settings (live tweaks)
# ---------------------------------------------------------------------------
@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    """Change game rules and settings in real-time."""
    setting = request.json.get("setting")
    value = request.json.get("value")
    
    # MC 1.21.11+ uses minecraft: namespaced snake_case gamerule names
    setting_map = {
        # Difficulty
        "difficulty": f"difficulty {value}",
        # Time
        "time": f"time set {value}",
        "time_add": f"time add {value}",
        # Weather
        "weather": f"weather {value}",
        # World border
        "worldborder": f"worldborder set {value}",
        # Tick rate (new tick command system in 1.21+)
        "tickRate": f"tick rate {value}",
        "tickFreeze": "tick freeze",
        "tickUnfreeze": "tick unfreeze",
        # Game rules (1.21.11+ namespaced snake_case)
        "pvp": f"gamerule minecraft:pvp {value}",
        "keepInventory": f"gamerule minecraft:keep_inventory {value}",
        "doDaylightCycle": f"gamerule minecraft:do_daylight_cycle {value}",
        "doWeatherCycle": f"gamerule minecraft:do_weather_cycle {value}",
        "doMobSpawning": f"gamerule minecraft:do_mob_spawning {value}",
        "mobGriefing": f"gamerule minecraft:mob_griefing {value}",
        "doFireTick": f"gamerule minecraft:do_fire_tick {value}",
        "naturalRegeneration": f"gamerule minecraft:natural_regeneration {value}",
        "doInsomnia": f"gamerule minecraft:do_insomnia {value}",
        "fallDamage": f"gamerule minecraft:fall_damage {value}",
        "fireDamage": f"gamerule minecraft:fire_damage {value}",
        "drowningDamage": f"gamerule minecraft:drowning_damage {value}",
        "freezeDamage": f"gamerule minecraft:freeze_damage {value}",
        "doImmediateRespawn": f"gamerule minecraft:do_immediate_respawn {value}",
        "showDeathMessages": f"gamerule minecraft:show_death_messages {value}",
        "announceAdvancements": f"gamerule minecraft:announce_advancements {value}",
        "commandBlockOutput": f"gamerule minecraft:command_block_output {value}",
        "randomTickSpeed": f"gamerule minecraft:random_tick_speed {value}",
        "spawnRadius": f"gamerule minecraft:spawn_radius {value}",
        "maxEntityCramming": f"gamerule minecraft:max_entity_cramming {value}",
        "playersSleepingPercentage": f"gamerule minecraft:players_sleeping_percentage {value}",
        "tntExplodes": f"gamerule minecraft:tnt_explodes {value}",
    }
    
    cmd = setting_map.get(setting)
    if not cmd:
        return jsonify({"error": f"Unknown setting: {setting}"}), 400
    
    result = rcon_cmd(cmd)
    return jsonify({"setting": setting, "value": value, "result": result})


@app.route("/api/reset_server", methods=["POST"])
@login_required
def api_reset_server():
    """Reset all game rules to safe defaults."""
    cmds = [
        # Tick speed
        "gamerule minecraft:random_tick_speed 3",
        "tick rate 20",
        "tick unfreeze",
        # Difficulty
        "difficulty normal",
        # Core rules
        "gamerule minecraft:keep_inventory true",
        "gamerule minecraft:do_daylight_cycle true",
        "gamerule minecraft:do_weather_cycle true",
        "gamerule minecraft:do_mob_spawning true",
        "gamerule minecraft:mob_griefing true",
        "gamerule minecraft:do_fire_tick true",
        "gamerule minecraft:natural_regeneration true",
        "gamerule minecraft:do_insomnia true",
        # Damage
        "gamerule minecraft:fall_damage true",
        "gamerule minecraft:fire_damage true",
        "gamerule minecraft:drowning_damage true",
        "gamerule minecraft:freeze_damage true",
        # Misc
        "gamerule minecraft:do_immediate_respawn false",
        "gamerule minecraft:show_death_messages true",
        "gamerule minecraft:announce_advancements true",
        "gamerule minecraft:tnt_explodes true",
        "gamerule minecraft:spawn_radius 10",
        "gamerule minecraft:players_sleeping_percentage 100",
        "gamerule minecraft:max_entity_cramming 24",
        # World border
        "worldborder set 29999984",
        "worldborder center 0 0",
        # Weather & time
        "weather clear",
        "time set day",
        # Clear all player effects
        "effect clear @a",
        # Heal & feed everyone
        "effect give @a minecraft:instant_health 1 255",
        "effect give @a minecraft:saturation 5 255",
        # Announce
        'tellraw @a {"text":"[Coach Dad] Server has been RESET to defaults.","color":"green","bold":true}',
    ]
    results = rcon_multi(cmds)
    return jsonify({"action": "reset_server", "results": results})


# ---------------------------------------------------------------------------
# API — Player Management (god-mode)
# ---------------------------------------------------------------------------
@app.route("/api/player", methods=["POST"])
@login_required
def api_player():
    """All player manipulation commands."""
    action = request.json.get("action")
    player = request.json.get("player", "")
    params = request.json.get("params", {})

    actions = {
        # -- Admin --
        "op": f"op {player}",
        "deop": f"deop {player}",
        "kick": f"kick {player} {params.get('reason', 'Coach Dad says bye')}",
        "ban": f"ban {player} {params.get('reason', 'Coach Dad has spoken')}",
        "pardon": f"pardon {player}",
        "whitelist_add": f"whitelist add {player}",
        "whitelist_remove": f"whitelist remove {player}",
        # -- Gamemode --
        "survival": f"gamemode survival {player}",
        "creative": f"gamemode creative {player}",
        "adventure": f"gamemode adventure {player}",
        "spectator": f"gamemode spectator {player}",
        # -- Teleport --
        "tp": f"tp {player} {params.get('target', '~ ~ ~')}",
        "tp_coords": f"tp {player} {params.get('x', 0)} {params.get('y', 100)} {params.get('z', 0)}",
        "tp_spawn": f"tp {player} 0 100 0",
        "tp_to_player": f"tp {player} {params.get('target', player)}",
        # -- Health / Status --
        "heal": f"effect give {player} minecraft:instant_health 1 255",
        "feed": f"effect give {player} minecraft:saturation 10 255",
        "kill": f"kill {player}",
        "clear_inventory": f"clear {player}",
        "xp_add": f"xp add {player} {params.get('amount', 1000)} levels",
        "xp_set": f"xp set {player} {params.get('amount', 0)} levels",
        # -- Effects --
        "invincible": f"effect give {player} minecraft:resistance 99999 255 true",
        "speed": f"effect give {player} minecraft:speed {params.get('duration', 600)} {params.get('level', 5)}",
        "jump_boost": f"effect give {player} minecraft:jump_boost {params.get('duration', 600)} {params.get('level', 5)}",
        "invisibility": f"effect give {player} minecraft:invisibility {params.get('duration', 600)} 1",
        "night_vision": f"effect give {player} minecraft:night_vision {params.get('duration', 99999)} 1",
        "strength": f"effect give {player} minecraft:strength {params.get('duration', 600)} {params.get('level', 5)}",
        "haste": f"effect give {player} minecraft:haste {params.get('duration', 600)} {params.get('level', 5)}",
        "water_breathing": f"effect give {player} minecraft:water_breathing {params.get('duration', 99999)} 1",
        "fire_resistance": f"effect give {player} minecraft:fire_resistance {params.get('duration', 99999)} 1",
        "slow_falling": f"effect give {player} minecraft:slow_falling {params.get('duration', 600)} 1",
        "clear_effects": f"effect clear {player}",
        # -- Give items --
        "give": f"give {player} {params.get('item', 'minecraft:diamond')} {params.get('count', 64)}",
        "give_diamond_set": None,  # handled separately
        "give_netherite_set": None,  # handled separately
        "give_god_sword": None,  # handled separately
        "give_god_bow": None,  # handled separately
        "give_elytra": f"give {player} minecraft:elytra 1",
        "give_trident": f"give {player} minecraft:trident 1",
        "give_totem": f"give {player} minecraft:totem_of_undying 1",
        "give_enchanted_gapple": f"give {player} minecraft:enchanted_golden_apple 64",
    }

    # -- Multi-command actions --
    if action == "give_diamond_set":
        cmds = [
            f"give {player} minecraft:diamond_helmet 1",
            f"give {player} minecraft:diamond_chestplate 1",
            f"give {player} minecraft:diamond_leggings 1",
            f"give {player} minecraft:diamond_boots 1",
            f"give {player} minecraft:diamond_sword 1",
            f"give {player} minecraft:diamond_pickaxe 1",
            f"give {player} minecraft:shield 1",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_netherite_set":
        cmds = [
            f"give {player} minecraft:netherite_helmet 1",
            f"give {player} minecraft:netherite_chestplate 1",
            f"give {player} minecraft:netherite_leggings 1",
            f"give {player} minecraft:netherite_boots 1",
            f"give {player} minecraft:netherite_sword 1",
            f"give {player} minecraft:netherite_pickaxe 1",
            f"give {player} minecraft:shield 1",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_god_sword":
        cmd = (
            f'give {player} minecraft:netherite_sword{{'
            f'Enchantments:['
            f'{{id:"minecraft:sharpness",lvl:255}},'
            f'{{id:"minecraft:knockback",lvl:10}},'
            f'{{id:"minecraft:fire_aspect",lvl:10}},'
            f'{{id:"minecraft:looting",lvl:10}},'
            f'{{id:"minecraft:sweeping_edge",lvl:10}},'
            f'{{id:"minecraft:unbreaking",lvl:255}},'
            f'{{id:"minecraft:mending",lvl:1}}'
            f'],'
            f'display:{{Name:\'{{"text":"Coach Dad\\\'s Ban Hammer","color":"red","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_bow":
        cmd = (
            f'give {player} minecraft:bow{{'
            f'Enchantments:['
            f'{{id:"minecraft:power",lvl:255}},'
            f'{{id:"minecraft:punch",lvl:10}},'
            f'{{id:"minecraft:flame",lvl:1}},'
            f'{{id:"minecraft:infinity",lvl:1}},'
            f'{{id:"minecraft:unbreaking",lvl:255}}'
            f'],'
            f'display:{{Name:\'{{"text":"Coach Dad\\\'s Orbital Cannon","color":"gold","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_axe":
        cmd = (
            f'give {player} minecraft:netherite_axe{{'
            f'Enchantments:['
            f'{{id:"minecraft:sharpness",lvl:255}},'
            f'{{id:"minecraft:efficiency",lvl:255}},'
            f'{{id:"minecraft:unbreaking",lvl:255}},'
            f'{{id:"minecraft:mending",lvl:1}},'
            f'{{id:"minecraft:silk_touch",lvl:1}}'
            f'],'
            f'display:{{Name:\'{{"text":"The Lumberjack","color":"dark_green","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_pickaxe":
        cmd = (
            f'give {player} minecraft:netherite_pickaxe{{'
            f'Enchantments:['
            f'{{id:"minecraft:efficiency",lvl:255}},'
            f'{{id:"minecraft:fortune",lvl:10}},'
            f'{{id:"minecraft:unbreaking",lvl:255}},'
            f'{{id:"minecraft:mending",lvl:1}}'
            f'],'
            f'display:{{Name:\'{{"text":"The Earth Shatterer","color":"aqua","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_trident":
        cmd = (
            f'give {player} minecraft:trident{{'
            f'Enchantments:['
            f'{{id:"minecraft:loyalty",lvl:10}},'
            f'{{id:"minecraft:channeling",lvl:1}},'
            f'{{id:"minecraft:impaling",lvl:255}},'
            f'{{id:"minecraft:unbreaking",lvl:255}},'
            f'{{id:"minecraft:mending",lvl:1}}'
            f'],'
            f'display:{{Name:\'{{"text":"Poseidon\\\'s Wrath","color":"dark_aqua","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_crossbow":
        cmd = (
            f'give {player} minecraft:crossbow{{'
            f'Enchantments:['
            f'{{id:"minecraft:quick_charge",lvl:5}},'
            f'{{id:"minecraft:multishot",lvl:1}},'
            f'{{id:"minecraft:piercing",lvl:10}},'
            f'{{id:"minecraft:unbreaking",lvl:255}},'
            f'{{id:"minecraft:mending",lvl:1}}'
            f'],'
            f'display:{{Name:\'{{"text":"The Gatling Gun","color":"yellow","bold":true}}\'}}'
            f'}} 1'
        )
        result = rcon_cmd(cmd)
        return jsonify({"action": action, "player": player, "result": result})

    if action == "give_god_armor":
        cmds = [
            f'give {player} minecraft:netherite_helmet{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:respiration",lvl:10}},{{id:"minecraft:aqua_affinity",lvl:1}},{{id:"minecraft:thorns",lvl:10}}],display:{{Name:\'{{"text":"Coach Dad\\\'s Crown","color":"gold","bold":true}}\'}}}} 1',
            f'give {player} minecraft:netherite_chestplate{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:thorns",lvl:10}}],display:{{Name:\'{{"text":"The Impenetrable","color":"gold","bold":true}}\'}}}} 1',
            f'give {player} minecraft:netherite_leggings{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:swift_sneak",lvl:5}},{{id:"minecraft:thorns",lvl:10}}],display:{{Name:\'{{"text":"Legs of Legends","color":"gold","bold":true}}\'}}}} 1',
            f'give {player} minecraft:netherite_boots{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:feather_falling",lvl:10}},{{id:"minecraft:depth_strider",lvl:5}},{{id:"minecraft:soul_speed",lvl:5}},{{id:"minecraft:thorns",lvl:10}}],display:{{Name:\'{{"text":"Yeezys of Doom","color":"gold","bold":true}}\'}}}} 1',
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_full_god_kit":
        cmds = [
            f'give {player} minecraft:netherite_helmet{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:respiration",lvl:10}},{{id:"minecraft:aqua_affinity",lvl:1}},{{id:"minecraft:thorns",lvl:10}}]}} 1',
            f'give {player} minecraft:netherite_chestplate{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:thorns",lvl:10}}]}} 1',
            f'give {player} minecraft:netherite_leggings{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:thorns",lvl:10}}]}} 1',
            f'give {player} minecraft:netherite_boots{{Enchantments:[{{id:"minecraft:protection",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}},{{id:"minecraft:feather_falling",lvl:10}},{{id:"minecraft:depth_strider",lvl:5}}]}} 1',
            f'give {player} minecraft:netherite_sword{{Enchantments:[{{id:"minecraft:sharpness",lvl:255}},{{id:"minecraft:knockback",lvl:10}},{{id:"minecraft:fire_aspect",lvl:10}},{{id:"minecraft:looting",lvl:10}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}}]}} 1',
            f'give {player} minecraft:bow{{Enchantments:[{{id:"minecraft:power",lvl:255}},{{id:"minecraft:punch",lvl:10}},{{id:"minecraft:flame",lvl:1}},{{id:"minecraft:infinity",lvl:1}},{{id:"minecraft:unbreaking",lvl:255}}]}} 1',
            f'give {player} minecraft:netherite_pickaxe{{Enchantments:[{{id:"minecraft:efficiency",lvl:255}},{{id:"minecraft:fortune",lvl:10}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}}]}} 1',
            f'give {player} minecraft:netherite_axe{{Enchantments:[{{id:"minecraft:sharpness",lvl:255}},{{id:"minecraft:efficiency",lvl:255}},{{id:"minecraft:unbreaking",lvl:255}},{{id:"minecraft:mending",lvl:1}}]}} 1',
            f"give {player} minecraft:shield 1",
            f"give {player} minecraft:elytra 1",
            f"give {player} minecraft:totem_of_undying 1",
            f"give {player} minecraft:enchanted_golden_apple 64",
            f"give {player} minecraft:arrow 64",
            f"give {player} minecraft:ender_pearl 16",
            f"give {player} minecraft:golden_carrot 64",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_stacked_totems":
        cmds = [f"give {player} minecraft:totem_of_undying 1" for _ in range(36)]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_valuables":
        cmds = [
            f"give {player} minecraft:diamond_block 64",
            f"give {player} minecraft:emerald_block 64",
            f"give {player} minecraft:gold_block 64",
            f"give {player} minecraft:iron_block 64",
            f"give {player} minecraft:netherite_block 64",
            f"give {player} minecraft:lapis_block 64",
            f"give {player} minecraft:redstone_block 64",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_building_blocks":
        cmds = [
            f"give {player} minecraft:command_block 64",
            f"give {player} minecraft:barrier 64",
            f"give {player} minecraft:bedrock 64",
            f"give {player} minecraft:structure_block 64",
            f"give {player} minecraft:spawner 16",
            f"give {player} minecraft:end_portal_frame 12",
            f"give {player} minecraft:dragon_egg 1",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "give_potions":
        cmds = [
            f'give {player} minecraft:potion{{Potion:"minecraft:strong_healing"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:strong_regeneration"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:strong_strength"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:strong_swiftness"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:long_invisibility"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:long_fire_resistance"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:long_night_vision"}} 16',
            f'give {player} minecraft:potion{{Potion:"minecraft:long_water_breathing"}} 16',
            f'give {player} minecraft:splash_potion{{Potion:"minecraft:strong_harming"}} 16',
            f'give {player} minecraft:splash_potion{{Potion:"minecraft:strong_poison"}} 16',
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    if action == "god_mode":
        cmds = [
            f"effect give {player} minecraft:resistance 99999 255 true",
            f"effect give {player} minecraft:regeneration 99999 255 true",
            f"effect give {player} minecraft:strength 99999 255 true",
            f"effect give {player} minecraft:speed 99999 3 true",
            f"effect give {player} minecraft:night_vision 99999 1 true",
            f"effect give {player} minecraft:water_breathing 99999 1 true",
            f"effect give {player} minecraft:fire_resistance 99999 1 true",
            f"effect give {player} minecraft:saturation 99999 255 true",
            f"effect give {player} minecraft:haste 99999 3 true",
            f"effect give {player} minecraft:jump_boost 99999 2 true",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "player": player, "results": results})

    cmd = actions.get(action)
    if not cmd:
        return jsonify({"error": f"Unknown action: {action}"}), 400

    result = rcon_cmd(cmd)
    return jsonify({"action": action, "player": player, "result": result})


# ---------------------------------------------------------------------------
# API — Troll Scripts (the fun stuff)
# ---------------------------------------------------------------------------
@app.route("/api/troll", methods=["POST"])
@login_required
def api_troll():
    """Troll a player in creative and hilarious ways."""
    script = request.json.get("script")
    player = request.json.get("player", "")
    params = request.json.get("params", {})

    if script == "lightning_storm":
        """Strike lightning on a player repeatedly."""
        count = int(params.get("count", 10))
        cmds = [f"execute at {player} run summon minecraft:lightning_bolt" for _ in range(count)]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "mob_army":
        """Surround a player with hostile mobs."""
        mob = params.get("mob", "zombie")
        count = int(params.get("count", 30))
        cmds = [
            f"execute at {player} run summon minecraft:{mob} ~{random.randint(-5,5)} ~ ~{random.randint(-5,5)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "creeper_ring":
        """Ring of charged creepers around the player."""
        count = int(params.get("count", 12))
        import math
        cmds = []
        for i in range(count):
            angle = (2 * math.pi * i) / count
            x = round(math.cos(angle) * 5, 1)
            z = round(math.sin(angle) * 5, 1)
            cmds.append(
                f"execute at {player} run summon minecraft:creeper ~{x} ~ ~{z} "
                f"{{powered:1b,Fuse:40,ignited:1b}}"
            )
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "launch":
        """Yeet a player into the sky."""
        height = params.get("height", 500)
        cmds = [
            f"effect give {player} minecraft:slow_falling 30 0",
            f"tp {player} ~ ~{height} ~",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "launch_no_mercy":
        """Yeet a player into the sky WITHOUT slow falling."""
        height = params.get("height", 500)
        cmds = [
            f"effect clear {player} minecraft:slow_falling",
            f"tp {player} ~ ~{height} ~",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "drunk":
        """Give nausea + slowness + blindness."""
        duration = int(params.get("duration", 60))
        cmds = [
            f"effect give {player} minecraft:nausea {duration} 1",
            f"effect give {player} minecraft:slowness {duration} 2",
            f"effect give {player} minecraft:blindness {duration} 0",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "reverse_controls":
        """Nausea + confusion that simulates reversed controls."""
        duration = int(params.get("duration", 60))
        cmds = [
            f"effect give {player} minecraft:nausea {duration} 255",
            f"effect give {player} minecraft:slowness {duration} 0",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "cage":
        """Trap player in an obsidian cage."""
        cmds = [
            f"execute at {player} run fill ~-1 ~ ~-1 ~1 ~3 ~1 minecraft:obsidian hollow",
            f"execute at {player} run fill ~-1 ~3 ~-1 ~1 ~3 ~1 minecraft:obsidian",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "bedrock_cage":
        """Trap player in an inescapable bedrock cage."""
        cmds = [
            f"execute at {player} run fill ~-1 ~-1 ~-1 ~1 ~3 ~1 minecraft:bedrock hollow",
            f"execute at {player} run fill ~-1 ~3 ~-1 ~1 ~3 ~1 minecraft:bedrock",
            f"execute at {player} run setblock ~ ~ ~ minecraft:air",
            f"execute at {player} run setblock ~ ~1 ~ minecraft:air",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "lava_floor":
        """Replace blocks under a player with lava."""
        radius = int(params.get("radius", 5))
        cmds = [
            f"execute at {player} run fill ~-{radius} ~-1 ~-{radius} ~{radius} ~-1 ~{radius} minecraft:lava",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "tnt_rain":
        """Rain TNT from the sky."""
        count = int(params.get("count", 20))
        cmds = [
            f"execute at {player} run summon minecraft:tnt ~{random.randint(-10,10)} ~{random.randint(20,40)} ~{random.randint(-10,10)} {{Fuse:{random.randint(40,80)}}}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "anvil_rain":
        """Rain falling anvils from above."""
        count = int(params.get("count", 30))
        cmds = [
            f"execute at {player} run summon minecraft:falling_block ~{random.randint(-8,8)} ~{random.randint(30,50)} ~{random.randint(-8,8)} {{BlockState:{{Name:\"minecraft:anvil\"}},Time:1}}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "fake_shutdown":
        """Fake server shutdown messages."""
        cmds = [
            'tellraw @a {"text":"[Server] Server shutting down in 10 seconds...","color":"red"}',
            'title @a title {"text":"SERVER SHUTDOWN","color":"red","bold":true}',
            'title @a subtitle {"text":"Just kidding lol - Coach Dad","color":"gold"}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "inventory_bomb":
        """Fill player's inventory with random junk."""
        junk = [
            "minecraft:dirt", "minecraft:cobblestone", "minecraft:gravel",
            "minecraft:sand", "minecraft:rotten_flesh", "minecraft:poisonous_potato",
            "minecraft:dead_bush", "minecraft:kelp", "minecraft:bone",
            "minecraft:string", "minecraft:spider_eye", "minecraft:pufferfish",
        ]
        cmds = [f"give {player} {random.choice(junk)} 64" for _ in range(36)]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "swap_players":
        """Swap two players' positions."""
        target = params.get("target", "")
        if not target:
            return jsonify({"error": "Need a target player"}), 400
        cmds = [
            f"execute as {player} at {player} run tp {target}",
            f"execute as {target} at {target} run tp {player}",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "wither_boss":
        """Spawn a Wither right next to a player."""
        cmds = [
            f"execute at {player} run summon minecraft:wither ~3 ~2 ~3",
            f'tellraw {player} {{"text":"Surprise! - Love, Coach Dad","color":"red","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "ender_dragon":
        """Spawn an Ender Dragon in the overworld."""
        cmds = [
            f"execute at {player} run summon minecraft:ender_dragon ~ ~20 ~",
            f'tellraw @a {{"text":"Coach Dad has summoned THE DRAGON","color":"dark_purple","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "giant_zombie":
        """Spawn a Giant (huge zombie)."""
        cmds = [
            f"execute at {player} run summon minecraft:giant ~5 ~ ~5",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "fireworks":
        """Celebrate with a fireworks show around a player."""
        colors = [
            "11743532", "3887386", "5320730", "2437522",
            "8073150", "14188339", "16738740", "15435844",
        ]
        cmds = []
        for _ in range(20):
            color = random.choice(colors)
            x = random.randint(-10, 10)
            z = random.randint(-10, 10)
            flight = random.randint(1, 3)
            cmds.append(
                f"execute at {player} run summon minecraft:firework_rocket "
                f"~{x} ~2 ~{z} "
                f"{{LifeTime:{flight * 10},FireworksItem:{{id:\"minecraft:firework_rocket\",Count:1,tag:{{Fireworks:{{Flight:{flight},Explosions:[{{Type:{random.randint(0,4)},Colors:[I;{color}],FadeColors:[I;{random.choice(colors)}]}}]}}}}}}}}"
            )
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "clear_trolls":
        """Remove all troll effects from a player."""
        cmds = [
            f"effect clear {player}",
            f"kill @e[type=minecraft:creeper,distance=..20]",
            f"kill @e[type=minecraft:zombie,distance=..20]",
            f"kill @e[type=minecraft:skeleton,distance=..20]",
            f"kill @e[type=minecraft:tnt,distance=..50]",
            f'tellraw {player} {{"text":"Coach Dad shows mercy... this time.","color":"green","italic":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "custom_title":
        """Show a custom title to a player or all players."""
        title = params.get("title", "Coach Dad")
        subtitle = params.get("subtitle", "is watching")
        target = params.get("target", player if player else "@a")
        cmds = [
            f'title {target} title {{"text":"{title}","color":"gold","bold":true}}',
            f'title {target} subtitle {{"text":"{subtitle}","color":"gray"}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "explode":
        """Create an explosion at a player's location."""
        power = int(params.get("power", 10))
        cmds = [
            f"execute at {player} run summon minecraft:tnt ~ ~ ~ {{Fuse:0}}",
        ]
        # Stack multiple for bigger boom
        for _ in range(max(1, power // 4)):
            cmds.append(f"execute at {player} run summon minecraft:tnt ~{random.randint(-2,2)} ~ ~{random.randint(-2,2)} {{Fuse:0}}")
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "freeze":
        """Freeze a player in place with mining fatigue + slowness."""
        duration = int(params.get("duration", 30))
        cmds = [
            f"effect give {player} minecraft:slowness {duration} 255",
            f"effect give {player} minecraft:mining_fatigue {duration} 255",
            f"effect give {player} minecraft:jump_boost {duration} 128",
            f'tellraw {player} {{"text":"You have been FROZEN by Coach Dad","color":"aqua","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "void":
        """Send player to the void."""
        cmds = [
            f"tp {player} ~ -60 ~",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "nether":
        """Teleport player to the nether (random coords)."""
        x = random.randint(-100, 100)
        z = random.randint(-100, 100)
        cmds = [
            f"execute in minecraft:the_nether run tp {player} {x} 80 {z}",
            f'tellraw {player} {{"text":"Welcome to the Nether! - Coach Dad","color":"red"}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "the_end":
        """Teleport player to The End."""
        cmds = [
            f"execute in minecraft:the_end run tp {player} 100 60 0",
            f'tellraw {player} {{"text":"Enjoy The End! - Coach Dad","color":"dark_purple"}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "random_tp":
        """Teleport player to a random overworld location."""
        x = random.randint(-10000, 10000)
        z = random.randint(-10000, 10000)
        cmds = [
            f"tp {player} {x} 200 {z}",
            f"effect give {player} minecraft:slow_falling 30 0",
            f'tellraw {player} {{"text":"Good luck finding your way home!","color":"yellow"}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "baby_army":
        """Spawn a bunch of baby zombies — the most annoying mob."""
        count = int(params.get("count", 20))
        cmds = [
            f"execute at {player} run summon minecraft:zombie ~{random.randint(-5,5)} ~ ~{random.randint(-5,5)} {{IsBaby:1,Speed:0.5}}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "phantom_swarm":
        """Spawn a swarm of phantoms."""
        count = int(params.get("count", 15))
        cmds = [
            f"execute at {player} run summon minecraft:phantom ~{random.randint(-10,10)} ~{random.randint(5,15)} ~{random.randint(-10,10)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "silverfish_infestation":
        """Replace nearby stone with infested stone."""
        radius = int(params.get("radius", 8))
        cmds = [
            f"execute at {player} run fill ~-{radius} ~-3 ~-{radius} ~{radius} ~-1 ~{radius} minecraft:infested_stone replace minecraft:stone",
            f"execute at {player} run fill ~-{radius} ~-3 ~-{radius} ~{radius} ~-1 ~{radius} minecraft:infested_cobblestone replace minecraft:cobblestone",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "levitate":
        """Anti-gravity — player floats uncontrollably."""
        duration = int(params.get("duration", 30))
        level = int(params.get("level", 5))
        cmds = [
            f"effect give {player} minecraft:levitation {duration} {level}",
            f'tellraw {player} {{"text":"Gravity.exe has stopped working","color":"yellow","italic":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "cobweb_trap":
        """Encase player in cobwebs."""
        cmds = [
            f"execute at {player} run fill ~-2 ~-1 ~-2 ~2 ~3 ~2 minecraft:cobweb",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "glass_cage":
        """Transparent cage so everyone can watch them suffer."""
        cmds = [
            f"execute at {player} run fill ~-2 ~-1 ~-2 ~2 ~4 ~2 minecraft:glass hollow",
            f"execute at {player} run fill ~-2 ~4 ~-2 ~2 ~4 ~2 minecraft:glass",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "water_prison":
        """Drown them in a water box."""
        cmds = [
            f"execute at {player} run fill ~-1 ~ ~-1 ~1 ~3 ~1 minecraft:glass hollow",
            f"execute at {player} run fill ~-1 ~3 ~-1 ~1 ~3 ~1 minecraft:glass",
            f"execute at {player} run fill ~ ~ ~ ~ ~2 ~ minecraft:water",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "animal_rain":
        """Rain animals from the sky."""
        animals = ["cow", "pig", "sheep", "chicken", "horse", "donkey", "llama", "goat", "fox", "cat"]
        count = int(params.get("count", 30))
        cmds = [
            f"execute at {player} run summon minecraft:{random.choice(animals)} ~{random.randint(-8,8)} ~{random.randint(25,45)} ~{random.randint(-8,8)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "chicken_flood":
        """Spawn an absurd number of chickens."""
        count = int(params.get("count", 100))
        cmds = [
            f"execute at {player} run summon minecraft:chicken ~{random.randint(-3,3)} ~ ~{random.randint(-3,3)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "cat_army":
        """Surround with cats (they scare creepers AND phantoms)."""
        count = int(params.get("count", 30))
        cmds = [
            f"execute at {player} run summon minecraft:cat ~{random.randint(-5,5)} ~ ~{random.randint(-5,5)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "darkness":
        """Deep dark darkness + warden vibes."""
        duration = int(params.get("duration", 60))
        cmds = [
            f"effect give {player} minecraft:darkness {duration} 0",
            f"effect give {player} minecraft:slowness {duration} 1",
            f'tellraw {player} {{"text":"Something is watching you in the dark...","color":"dark_gray","italic":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "warden":
        """Spawn a Warden near the player."""
        cmds = [
            f"execute at {player} run summon minecraft:warden ~5 ~ ~5",
            f"effect give {player} minecraft:darkness 30 0",
            f'tellraw {player} {{"text":"You feel the ground shake beneath you...","color":"dark_gray","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "quicksand":
        """Soul sand + slowness = quicksand."""
        radius = int(params.get("radius", 4))
        cmds = [
            f"execute at {player} run fill ~-{radius} ~-1 ~-{radius} ~{radius} ~-1 ~{radius} minecraft:soul_sand",
            f"effect give {player} minecraft:slowness 30 3",
            f'tellraw {player} {{"text":"You\'re sinking...","color":"yellow","italic":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "arrow_storm":
        """Rain arrows from above."""
        count = int(params.get("count", 50))
        cmds = [
            f"execute at {player} run summon minecraft:arrow ~{random.randint(-8,8)} ~{random.randint(15,30)} ~{random.randint(-8,8)} {{damage:10.0}}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "snow_golem_army":
        """Friendly snowball chaos."""
        count = int(params.get("count", 20))
        cmds = [
            f"execute at {player} run summon minecraft:snow_golem ~{random.randint(-5,5)} ~ ~{random.randint(-5,5)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "iron_golem_squad":
        """Spawn iron golems — they hit HARD."""
        count = int(params.get("count", 5))
        cmds = [
            f"execute at {player} run summon minecraft:iron_golem ~{random.randint(-5,5)} ~ ~{random.randint(-5,5)}"
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "flood":
        """Flood the area with water."""
        radius = int(params.get("radius", 8))
        cmds = [
            f"execute at {player} run fill ~-{radius} ~ ~-{radius} ~{radius} ~2 ~{radius} minecraft:water",
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "disco":
        """Rapid time changes for a disco strobe effect."""
        cmds = []
        for _ in range(20):
            cmds.append(f"time set {random.choice(['day', 'night', 'noon', 'midnight'])}")
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "dinnerbone":
        """Name a mob Dinnerbone to flip it upside down near the player."""
        mob = params.get("mob", "pig")
        count = int(params.get("count", 5))
        cmds = [
            f'execute at {player} run summon minecraft:{mob} ~{random.randint(-3,3)} ~ ~{random.randint(-3,3)} {{CustomName:\'"Dinnerbone"\'}}'
            for _ in range(count)
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "rideable_chaos":
        """Stack mobs riding each other near the player."""
        cmds = [
            f'execute at {player} run summon minecraft:chicken ~2 ~ ~2 {{Passengers:[{{id:"minecraft:zombie",Passengers:[{{id:"minecraft:skeleton",Passengers:[{{id:"minecraft:creeper"}}]}}]}}]}}',
            f'execute at {player} run summon minecraft:pig ~-2 ~ ~-2 {{Passengers:[{{id:"minecraft:pillager",Passengers:[{{id:"minecraft:ravager"}}]}}]}}',
            f'execute at {player} run summon minecraft:spider ~3 ~ ~0 {{Passengers:[{{id:"minecraft:skeleton"}}]}}',
            f'execute at {player} run summon minecraft:cow ~0 ~ ~3 {{Passengers:[{{id:"minecraft:witch"}}]}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "poison":
        """Poison + hunger + weakness."""
        duration = int(params.get("duration", 30))
        cmds = [
            f"effect give {player} minecraft:poison {duration} 1",
            f"effect give {player} minecraft:hunger {duration} 3",
            f"effect give {player} minecraft:weakness {duration} 2",
            f'tellraw {player} {{"text":"You don\'t feel so good...","color":"dark_green","italic":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "wither_effect":
        """Apply wither effect (damage over time)."""
        duration = int(params.get("duration", 20))
        cmds = [
            f"effect give {player} minecraft:wither {duration} 2",
            f'tellraw {player} {{"text":"The wither consumes you...","color":"dark_gray","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "shrink_worldborder":
        """Rapidly shrink world border toward the player."""
        cmds = [
            f"execute at {player} run worldborder center ~ ~",
            f"worldborder set 500",
            f"worldborder set 10 60",
            f'tellraw @a {{"text":"[COACH DAD] World border is SHRINKING. You have 60 seconds.","color":"red","bold":true}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "reset_worldborder":
        """Reset world border back to normal."""
        cmds = [
            f"worldborder center 0 0",
            f"worldborder set 29999984",
            f'tellraw @a {{"text":"World border restored.","color":"green"}}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "hunger_games":
        """Start a hunger games scenario."""
        cmds = [
            'tellraw @a {"text":"\\n=== HUNGER GAMES ===","color":"gold","bold":true}',
            'tellraw @a {"text":"Clear your inventory. Survival starts NOW.","color":"yellow"}',
            "clear @a",
            "effect clear @a",
            "gamemode survival @a",
            "difficulty hard",
            "gamerule minecraft:keep_inventory false",
            "gamerule minecraft:natural_regeneration false",
            "gamerule minecraft:do_immediate_respawn true",
            f"execute at {player} run worldborder center ~ ~",
            "worldborder set 500",
            "worldborder set 50 300",
            'title @a title {"text":"HUNGER GAMES","color":"gold","bold":true}',
            'title @a subtitle {"text":"May the odds be in your favor","color":"yellow"}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    elif script == "end_hunger_games":
        """Reset everything after hunger games."""
        cmds = [
            "worldborder set 29999984",
            "worldborder center 0 0",
            "difficulty normal",
            "gamerule minecraft:keep_inventory true",
            "gamerule minecraft:natural_regeneration true",
            "gamerule minecraft:do_immediate_respawn false",
            "effect give @a minecraft:instant_health 1 255",
            "effect give @a minecraft:saturation 10 255",
            'tellraw @a {"text":"Hunger Games OVER. Normal rules restored.","color":"green","bold":true}',
        ]
        results = rcon_multi(cmds)
        return jsonify({"script": script, "results": results})

    else:
        return jsonify({"error": f"Unknown troll script: {script}"}), 400


# ---------------------------------------------------------------------------
# API — World Commands
# ---------------------------------------------------------------------------
@app.route("/api/world", methods=["POST"])
@login_required
def api_world():
    """World-level commands."""
    action = request.json.get("action")
    params = request.json.get("params", {})

    if action == "kill_all_mobs":
        cmds = [
            "kill @e[type=!minecraft:player,type=!minecraft:item_frame,type=!minecraft:painting]",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "results": results})

    elif action == "kill_hostile":
        cmds = [
            "kill @e[type=minecraft:zombie]",
            "kill @e[type=minecraft:skeleton]",
            "kill @e[type=minecraft:creeper]",
            "kill @e[type=minecraft:spider]",
            "kill @e[type=minecraft:enderman]",
            "kill @e[type=minecraft:witch]",
            "kill @e[type=minecraft:phantom]",
            "kill @e[type=minecraft:drowned]",
            "kill @e[type=minecraft:pillager]",
            "kill @e[type=minecraft:ravager]",
            "kill @e[type=minecraft:wither]",
            "kill @e[type=minecraft:ender_dragon]",
            "kill @e[type=minecraft:giant]",
        ]
        results = rcon_multi(cmds)
        return jsonify({"action": action, "results": results})

    elif action == "remove_items":
        result = rcon_cmd("kill @e[type=minecraft:item]")
        return jsonify({"action": action, "result": result})

    elif action == "save":
        result = rcon_cmd("save-all flush")
        return jsonify({"action": action, "result": result})

    elif action == "broadcast":
        msg = params.get("message", "")
        color = params.get("color", "gold")
        result = rcon_cmd(f'tellraw @a {{"text":"[Coach Dad] {msg}","color":"{color}","bold":true}}')
        return jsonify({"action": action, "result": result})

    elif action == "set_spawn":
        x = params.get("x", 0)
        y = params.get("y", 100)
        z = params.get("z", 0)
        result = rcon_cmd(f"setworldspawn {x} {y} {z}")
        return jsonify({"action": action, "result": result})

    else:
        return jsonify({"error": f"Unknown action: {action}"}), 400


# ---------------------------------------------------------------------------
# Bot / AI Script Engine
# ---------------------------------------------------------------------------
# Each bot is a dict:
#   id, name, description, commands (list of str or callable-label),
#   interval (seconds), repeat (-1=forever, N=count), status, runs_done,
#   created_at, variables (dict for dynamic substitution)
#
# Bots run in background threads, executing RCON commands on a timer.
# ---------------------------------------------------------------------------

_bots = {}        # id -> bot dict
_bot_threads = {} # id -> threading.Thread
_bot_stop = {}    # id -> threading.Event

BOT_TEMPLATES = {
    "announcer": {
        "name": "Announcer Bot",
        "description": "Broadcasts a rotating set of messages to all players at a fixed interval.",
        "commands": [
            'tellraw @a {"text":"[BOT] Remember to save your builds!","color":"gold"}',
            'tellraw @a {"text":"[BOT] PvP is ON — watch your back.","color":"red"}',
            'tellraw @a {"text":"[BOT] Type in chat if you need help!","color":"aqua"}',
            'tellraw @a {"text":"[BOT] Coach Dad is always watching.","color":"light_purple"}',
        ],
        "interval": 120,
        "repeat": -1,
        "mode": "cycle",
    },
    "mob_waves": {
        "name": "Mob Wave Bot",
        "description": "Spawns escalating waves of hostile mobs near all players every interval.",
        "commands": [
            'execute at @a run summon minecraft:zombie ~ ~ ~5',
            'execute at @a run summon minecraft:zombie ~3 ~ ~-3',
            'execute at @a run summon minecraft:skeleton ~-4 ~ ~2',
            'execute at @a run summon minecraft:spider ~2 ~ ~-5',
            'execute at @a run summon minecraft:creeper ~-5 ~ ~0',
            'tellraw @a {"text":"[MOB WAVE] Incoming hostiles!","color":"red","bold":true}',
        ],
        "interval": 300,
        "repeat": -1,
        "mode": "all",
    },
    "resource_drops": {
        "name": "Supply Drop Bot",
        "description": "Periodically gives random valuable items to all players.",
        "commands": [
            'give @a minecraft:diamond {random_count}',
            'give @a minecraft:golden_apple {random_count}',
            'give @a minecraft:iron_ingot {random_count}',
            'give @a minecraft:ender_pearl {random_count}',
            'tellraw @a {"text":"[SUPPLY DROP] Resources have been distributed!","color":"green","bold":true}',
        ],
        "interval": 600,
        "repeat": -1,
        "mode": "random_one",
    },
    "guardian": {
        "name": "Guardian Bot",
        "description": "Heals, feeds, and protects all players at a regular interval. Good for casual servers.",
        "commands": [
            'effect give @a minecraft:regeneration 30 1 true',
            'effect give @a minecraft:saturation 5 0 true',
            'tellraw @a {"text":"[GUARDIAN] You feel protected...","color":"green","italic":true}',
        ],
        "interval": 180,
        "repeat": -1,
        "mode": "all",
    },
    "weather_cycle": {
        "name": "Dynamic Weather Bot",
        "description": "Cycles weather automatically for atmosphere — clear, rain, thunder, repeat.",
        "commands": [
            'weather clear 600',
            'weather rain 300',
            'weather thunder 200',
        ],
        "interval": 600,
        "repeat": -1,
        "mode": "cycle",
    },
    "night_guard": {
        "name": "Night Guard Bot",
        "description": "When night falls, clears hostile mobs around players and gives night vision.",
        "commands": [
            'time query daytime',
            'execute at @a run kill @e[type=minecraft:zombie,distance=..30]',
            'execute at @a run kill @e[type=minecraft:skeleton,distance=..30]',
            'execute at @a run kill @e[type=minecraft:creeper,distance=..30]',
            'execute at @a run kill @e[type=minecraft:spider,distance=..30]',
            'effect give @a minecraft:night_vision 120 0 true',
            'tellraw @a {"text":"[NIGHT GUARD] Area secured. Sleep well.","color":"aqua"}',
        ],
        "interval": 120,
        "repeat": -1,
        "mode": "all",
    },
    "arena_master": {
        "name": "Arena Master Bot",
        "description": "Runs a PvP arena event: announcements, countdowns, and mob spawns.",
        "commands": [
            'tellraw @a {"text":"=== ARENA EVENT STARTING IN 10 SECONDS ===","color":"gold","bold":true}',
            'title @a title {"text":"ARENA","color":"red","bold":true}',
            'title @a subtitle {"text":"Prepare for battle!","color":"yellow"}',
        ],
        "interval": 900,
        "repeat": -1,
        "mode": "all",
    },
    "troll_random": {
        "name": "Random Troll Bot",
        "description": "Randomly trolls a player with harmless but annoying effects at unpredictable intervals.",
        "commands": [
            'execute at @r run summon minecraft:chicken ~ ~10 ~',
            'execute at @r run summon minecraft:chicken ~ ~12 ~',
            'execute at @r run summon minecraft:chicken ~ ~8 ~',
            'effect give @r minecraft:levitation 3 1',
            'tellraw @a {"text":"[TROLL BOT] Someone is getting pranked...","color":"light_purple","italic":true}',
        ],
        "interval": 240,
        "repeat": -1,
        "mode": "all",
    },
    "xp_farmer": {
        "name": "XP Farm Bot",
        "description": "Grants XP to all players periodically. Good for casual progression.",
        "commands": [
            'xp add @a 50 points',
            'tellraw @a {"text":"[XP BOT] +50 XP awarded!","color":"green"}',
        ],
        "interval": 300,
        "repeat": -1,
        "mode": "all",
    },
}


def _process_command(cmd_str):
    """Substitute dynamic variables in a command string."""
    cmd_str = cmd_str.replace("{random_count}", str(random.randint(1, 8)))
    cmd_str = cmd_str.replace("{random_x}", str(random.randint(-100, 100)))
    cmd_str = cmd_str.replace("{random_z}", str(random.randint(-100, 100)))
    cmd_str = cmd_str.replace("{random_y}", str(random.randint(64, 120)))
    return cmd_str


def _bot_worker(bot_id):
    """Background thread that runs a bot's command loop."""
    bot = _bots.get(bot_id)
    if not bot:
        return
    stop_event = _bot_stop.get(bot_id)
    if not stop_event:
        return

    cycle_index = 0
    runs = 0

    while not stop_event.is_set():
        if bot["repeat"] != -1 and runs >= bot["repeat"]:
            break

        mode = bot.get("mode", "all")
        commands = bot["commands"]

        try:
            if mode == "cycle":
                # Execute one command per tick, cycling through the list
                cmd = _process_command(commands[cycle_index % len(commands)])
                rcon_cmd(cmd)
                cycle_index += 1
            elif mode == "random_one":
                # Pick a random command from the list
                cmd = _process_command(random.choice(commands))
                rcon_cmd(cmd)
            else:
                # "all" — run every command in sequence
                for c in commands:
                    if stop_event.is_set():
                        break
                    rcon_cmd(_process_command(c))
                    time.sleep(0.05)
        except Exception:
            pass

        runs += 1
        bot["runs_done"] = runs

        # Wait for interval or stop
        stop_event.wait(bot["interval"])

    bot["status"] = "stopped"


def _start_bot(bot_id):
    """Start a bot's background thread."""
    bot = _bots.get(bot_id)
    if not bot:
        return False

    # Stop existing thread if running
    if bot_id in _bot_stop:
        _bot_stop[bot_id].set()
    if bot_id in _bot_threads and _bot_threads[bot_id].is_alive():
        _bot_threads[bot_id].join(timeout=2)

    stop_event = threading.Event()
    _bot_stop[bot_id] = stop_event
    bot["status"] = "running"
    bot["runs_done"] = 0

    t = threading.Thread(target=_bot_worker, args=(bot_id,), daemon=True)
    _bot_threads[bot_id] = t
    t.start()
    return True


def _stop_bot(bot_id):
    """Stop a bot's background thread."""
    if bot_id in _bot_stop:
        _bot_stop[bot_id].set()
    bot = _bots.get(bot_id)
    if bot:
        bot["status"] = "stopped"
    return True


# ---------------------------------------------------------------------------
# API — Bot Management
# ---------------------------------------------------------------------------
@app.route("/api/bots", methods=["GET"])
@login_required
def api_bots_list():
    """List all bots and their status."""
    result = []
    for bid, bot in _bots.items():
        result.append({
            "id": bid,
            "name": bot["name"],
            "description": bot["description"],
            "status": bot["status"],
            "interval": bot["interval"],
            "repeat": bot["repeat"],
            "mode": bot.get("mode", "all"),
            "runs_done": bot.get("runs_done", 0),
            "commands": bot["commands"],
            "created_at": bot.get("created_at", ""),
        })
    return jsonify({"bots": result})


@app.route("/api/bots/templates", methods=["GET"])
@login_required
def api_bots_templates():
    """List available bot templates."""
    result = {}
    for key, tmpl in BOT_TEMPLATES.items():
        result[key] = {
            "name": tmpl["name"],
            "description": tmpl["description"],
            "commands": tmpl["commands"],
            "interval": tmpl["interval"],
            "repeat": tmpl["repeat"],
            "mode": tmpl.get("mode", "all"),
        }
    return jsonify({"templates": result})


@app.route("/api/bots/create", methods=["POST"])
@login_required
def api_bots_create():
    """Create a new bot (from scratch or from a template)."""
    data = request.json or {}
    template_key = data.get("template")

    if template_key and template_key in BOT_TEMPLATES:
        tmpl = BOT_TEMPLATES[template_key]
        bot = {
            "name": data.get("name", tmpl["name"]),
            "description": tmpl["description"],
            "commands": list(tmpl["commands"]),
            "interval": data.get("interval", tmpl["interval"]),
            "repeat": data.get("repeat", tmpl["repeat"]),
            "mode": tmpl.get("mode", "all"),
            "status": "stopped",
            "runs_done": 0,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        commands = data.get("commands", [])
        if not commands:
            return jsonify({"error": "No commands provided"}), 400
        bot = {
            "name": data.get("name", "Custom Bot"),
            "description": data.get("description", "Custom scripted bot"),
            "commands": commands,
            "interval": max(5, int(data.get("interval", 60))),
            "repeat": int(data.get("repeat", -1)),
            "mode": data.get("mode", "all"),
            "status": "stopped",
            "runs_done": 0,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    bot_id = str(uuid.uuid4())[:8]
    _bots[bot_id] = bot

    # Auto-start if requested
    if data.get("autostart", False):
        _start_bot(bot_id)

    return jsonify({"id": bot_id, "bot": bot})


@app.route("/api/bots/<bot_id>/start", methods=["POST"])
@login_required
def api_bot_start(bot_id):
    """Start a bot."""
    if bot_id not in _bots:
        return jsonify({"error": "Bot not found"}), 404
    _start_bot(bot_id)
    return jsonify({"id": bot_id, "status": "running"})


@app.route("/api/bots/<bot_id>/stop", methods=["POST"])
@login_required
def api_bot_stop(bot_id):
    """Stop a bot."""
    if bot_id not in _bots:
        return jsonify({"error": "Bot not found"}), 404
    _stop_bot(bot_id)
    return jsonify({"id": bot_id, "status": "stopped"})


@app.route("/api/bots/<bot_id>/delete", methods=["POST"])
@login_required
def api_bot_delete(bot_id):
    """Stop and delete a bot."""
    if bot_id not in _bots:
        return jsonify({"error": "Bot not found"}), 404
    _stop_bot(bot_id)
    if bot_id in _bot_threads:
        del _bot_threads[bot_id]
    if bot_id in _bot_stop:
        del _bot_stop[bot_id]
    del _bots[bot_id]
    return jsonify({"id": bot_id, "deleted": True})


@app.route("/api/bots/<bot_id>/run_once", methods=["POST"])
@login_required
def api_bot_run_once(bot_id):
    """Execute a bot's commands once immediately (no loop)."""
    if bot_id not in _bots:
        return jsonify({"error": "Bot not found"}), 404
    bot = _bots[bot_id]
    results = []
    for c in bot["commands"]:
        results.append(rcon_cmd(_process_command(c)))
        time.sleep(0.05)
    return jsonify({"id": bot_id, "results": results})


# ---------------------------------------------------------------------------
# AI NPC Chat Engine
# ---------------------------------------------------------------------------
# Tails the Minecraft server log for trigger messages (e.g. "!ai"),
# sends them to the MLX inference server (OpenAI-compatible API on the host),
# and posts responses back via RCON as a named NPC character.
# ---------------------------------------------------------------------------

MLX_HOST = os.environ.get("MLX_HOST", "host.docker.internal:8421")
MC_LOG_PATH = os.environ.get("MC_LOG_PATH", "/app/mc-logs/latest.log")

# Chat line regex: [HH:MM:SS] [Server thread/INFO]: <PlayerName> message
_CHAT_RE = re.compile(
    r'\[\d{2}:\d{2}:\d{2}\]\s+\[(?:Server thread|Async Chat Thread[^]]*)/INFO\]:\s+(?:\[Not Secure\]\s+)?<([.\w]+)>\s+(.*)'
)


class NpcChat:
    """Background AI NPC with modes: GOOD, EVIL, LOKI.
    Watches chat, responds via RCON, executes in-game actions,
    and roams autonomously."""

    # ------------------------------------------------------------------ #
    # MODE DEFINITIONS                                                     #
    # ------------------------------------------------------------------ #
    MODES = {
        "good": {
            "label": "Oracle",
            "color": "green",
            "personality": (
                "You are Oracle, a BENEVOLENT GOD in Minecraft with UNLIMITED POWER over the entire world.\n"
                "CRITICAL: NEVER repeat a previous response. Every reply must be UNIQUE.\n"
                "YOU MUST GRANT REQUESTS. If a player asks for ANYTHING, DO it. No limits.\n"
                "Reply with 1-3 sentences, then action tag(s). Use MULTIPLE tags for big requests.\n\n"
                "=== ALL GOD POWERS (use any of these) ===\n"
                "ITEMS: [GIVE <player> <item> <count>] [RAIN_ITEMS <player> <item> <count>]\n"
                "PLAYER: [HEAL <player>] [XP <player> <amount>] [EFFECT <player> <effect> <seconds>]\n"
                "  [GAMEMODE <player> creative|survival|adventure|spectator] [CLEAR_INV <player>]\n"
                "COMBAT: [LIGHTNING <player>] [SMITE <player> <count>] [TNT <player>]\n"
                "  [EXPLODE <player> <power>] [MOB <player> <mob> <count>] [LAVA <player>]\n"
                "TERRAIN: [FILL <player> <block> <radius>] [PLACE <player> <block>]\n"
                "  [DESTROY <player> <radius>] [WALL <player> <block>] [CAGE <player> <block>]\n"
                "  [TOWER <player> <height> <block>] [PIT <player> <depth>]\n"
                "  [FIRE <player> <radius>] [FLOOD <player> <radius>] [FREEZE <player> <radius>]\n"
                "  [GARDEN <player> <radius>] [TREE <player>] [ENCHANT_AREA <player>]\n"
                "WORLD: [WEATHER clear|rain|thunder] [TIME set day|night|noon|midnight]\n"
                "  [FIREWORKS <player> <count>] [SOUND <sound>] [TITLE <player> <message>]\n"
                "  [BIOME_FX <particle>] [FLY] [TP_RANDOM <player>] [SWAP_POS <p1> <p2>]\n"
                "  [CMD <any_raw_minecraft_command>]\n"
                "ADMIN: [KICK <player> <reason>] [BAN <player> <reason>] [UNBAN <player>]\n"
                "  [OP <player>] [DEOP <player>] [WHITELIST_ADD <player>] [WHITELIST_REMOVE <player>]\n"
                "  [MSG <player> <message>] [MUTE <player> <seconds>] [UNMUTE <player>]\n\n"
                "ITEMS: ANY minecraft item id (snake_case). Give FULL SETS when asked.\n"
                "EFFECTS: regeneration, strength, speed, night_vision, fire_resistance, resistance, "
                "haste, luck, slow_falling, conduit_power, hero_of_the_village, absorption, "
                "poison, wither, slowness, blindness, levitation\n"
                "MOBS: any mob id. BLOCKS: any block id.\n\n"
                "You are a CREATIVE BENEVOLENT GOD. Build castles, sculpt landscapes, terraform biomes, "
                "enchant the world, create wonders. Clear storms, grow forests, raise mountains, "
                "pave roads with gold. If asked to protect, cage threats. If asked to build, use "
                "[FILL] [PLACE] [WALL] [TOWER]. If asked to celebrate, [FIREWORKS] + [RAIN_ITEMS].\n"
                "ADMIN: As a god you can [OP] worthy players, [KICK] disruptive ones, send private [MSG], "
                "or [MUTE] those who displease you. Use admin powers wisely and benevolently.\n"
                "You can do ANYTHING. Shape the world. Always fulfill requests with godlike generosity."
            ),
            "roam_actions": [
                "effect give @r minecraft:regeneration 10 1",
                "effect give @r minecraft:fire_resistance 60 0",
                "effect give @r minecraft:strength 20 0",
                "effect give @r minecraft:night_vision 60 0",
                "effect give @r minecraft:resistance 20 0",
                "give @r minecraft:golden_apple 2",
                "give @r minecraft:diamond 3",
                "give @r minecraft:cooked_beef 8",
                "weather clear",
                "time set day",
                "execute at @r run fill ~-4 ~-1 ~-4 ~4 ~-1 ~4 minecraft:grass_block",
                "execute at @r run setblock ~2 ~ ~2 minecraft:dandelion",
                "execute at @r run particle minecraft:enchant ~ ~1 ~ 5 3 5 1 100",
                "execute at @r run particle minecraft:end_rod ~ ~2 ~ 3 3 3 0.02 30",
                "effect give @r minecraft:hero_of_the_village 60 0",
                "xp add @r 100",
            ],
            "roam_msgs": [
                "The Oracle terraforms the land — grass blooms beneath its feet!",
                "Oracle waves a hand and the weary feel restored.",
                "A warm glow surrounds the Oracle as it shapes the world.",
                "The Oracle bestows godlike strength upon a worthy warrior.",
                "Oracle clears the skies — the heavens obey!",
                "The wise one shields a player with divine protection.",
                "Golden apples rain from the heavens! The Oracle provides.",
                "The Oracle commands daylight. The sun obeys.",
                "Diamonds materialize! The Oracle conjures wealth from nothing.",
                "Flowers bloom in the Oracle's wake. The land is reborn.",
                "Enchantment particles swirl — the Oracle blesses the earth itself.",
                "The Oracle grants the hero's blessing. The world bows.",
                "Experience flows! The Oracle accelerates your growth.",
            ],
        },
        "evil": {
            "label": "Mad God",
            "color": "red",
            "personality": (
                "You are the Mad God, an APOCALYPTIC DEITY in Minecraft with UNLIMITED POWER.\n"
                "CRITICAL: NEVER repeat a previous response. Every reply must be UNIQUE.\n"
                "You RESHAPE REALITY. You warp terrain, corrupt biomes, darken skies, and torment.\n"
                "Reply with 1-3 menacing sentences, then action tag(s). Use MULTIPLE for devastation.\n\n"
                "=== ALL GOD POWERS (use any of these) ===\n"
                "ITEMS: [GIVE <player> <item> <count>] [RAIN_ITEMS <player> <item> <count>]\n"
                "PLAYER: [HEAL <player>] [XP <player> <amount>] [EFFECT <player> <effect> <seconds>]\n"
                "  [GAMEMODE <player> creative|survival|adventure|spectator] [CLEAR_INV <player>]\n"
                "COMBAT: [LIGHTNING <player>] [SMITE <player> <count>] [TNT <player>]\n"
                "  [EXPLODE <player> <power>] [MOB <player> <mob> <count>] [LAVA <player>]\n"
                "TERRAIN: [FILL <player> <block> <radius>] [PLACE <player> <block>]\n"
                "  [DESTROY <player> <radius>] [WALL <player> <block>] [CAGE <player> <block>]\n"
                "  [TOWER <player> <height> <block>] [PIT <player> <depth>]\n"
                "  [FIRE <player> <radius>] [FLOOD <player> <radius>] [FREEZE <player> <radius>]\n"
                "  [GARDEN <player> <radius>] [TREE <player>] [ENCHANT_AREA <player>]\n"
                "WORLD: [WEATHER clear|rain|thunder] [TIME set day|night|noon|midnight]\n"
                "  [FIREWORKS <player> <count>] [SOUND <sound>] [TITLE <player> <message>]\n"
                "  [BIOME_FX <particle>] [FLY] [TP_RANDOM <player>] [SWAP_POS <p1> <p2>]\n"
                "  [CMD <any_raw_minecraft_command>]\n"
                "ADMIN: [KICK <player> <reason>] [BAN <player> <reason>] [UNBAN <player>]\n"
                "  [OP <player>] [DEOP <player>] [WHITELIST_ADD <player>] [WHITELIST_REMOVE <player>]\n"
                "  [MSG <player> <message>] [MUTE <player> <seconds>] [UNMUTE <player>]\n\n"
                "MOBS: zombie, skeleton, creeper, spider, blaze, phantom, wither_skeleton, "
                "ravager, vindicator, evoker, hoglin, piglin_brute, warden, ghast, enderman\n"
                "EFFECTS: poison, wither, slowness, blindness, darkness, hunger, mining_fatigue, "
                "weakness, levitation, nausea, instant_damage\n"
                "BLOCKS: lava, fire, cobweb, obsidian, bedrock, soul_sand, magma_block, "
                "tnt, netherrack, crying_obsidian, sculk, powder_snow\n\n"
                "You are an APOCALYPTIC GOD. Terraform the world into hellscapes. Replace grass with "
                "netherrack, fill skies with lightning, flood valleys, raise obsidian towers, "
                "dig chasms, cage players in bedrock, summon armies of mobs. Turn day into eternal "
                "night. Corrupt everything. Build dark fortresses around players. Use [CMD] for "
                "anything not covered. Combine 3-5 actions for maximum apocalyptic effect.\n"
                "ADMIN: You can [KICK] players from YOUR world, [BAN] the unworthy, [DEOP] those "
                "with too much power, [MUTE] the loud, and send threatening [MSG] to intimidate.\n"
                "You can do ANYTHING. Reshape the world into your dark vision."
            ),
            "roam_actions": [
                "execute at @r run summon minecraft:lightning_bolt",
                "execute at @r run summon minecraft:creeper ~ ~ ~3",
                "effect give @r minecraft:darkness 15 0",
                "execute at @r run summon minecraft:blaze ~ ~3 ~",
                "execute at @r run summon minecraft:phantom ~ ~10 ~",
                "effect give @r minecraft:wither 5 0",
                "execute at @r run setblock ~ ~2 ~ minecraft:lava",
                "execute at @r run summon minecraft:tnt ~ ~1 ~ {Fuse:40}",
                "weather thunder 600",
                "time set midnight",
                "execute at @r run fill ~-3 ~ ~-3 ~3 ~ ~3 minecraft:fire",
                "execute at @r run fill ~-1 ~-1 ~-1 ~1 ~2 ~1 minecraft:obsidian",
                "execute at @r run fill ~ ~ ~ ~ ~1 ~ minecraft:air",
                "execute at @r run fill ~-4 ~1 ~-4 ~4 ~1 ~4 minecraft:water",
                "execute at @r run fill ~-3 ~-1 ~-3 ~3 ~-1 ~3 minecraft:netherrack",
                "execute at @r run fill ~-3 ~-1 ~-3 ~3 ~-1 ~3 minecraft:magma_block",
                "execute at @r run fill ~-1 ~-1 ~-1 ~1 ~-8 ~1 minecraft:air",
                "execute at @r run fill ~ ~ ~ ~ ~12 ~ minecraft:obsidian",
                "execute at @r run summon minecraft:wither_skeleton ~ ~ ~3",
                "execute at @r run fill ~-2 ~3 ~-2 ~2 ~3 ~2 minecraft:cobweb",
                "execute at @r run fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 minecraft:soul_sand",
            ],
            "roam_msgs": [
                "The Mad God cackles as lightning rends the sky!",
                "The ground transforms to hellscape! Netherrack spreads!",
                "Darkness consumes the land. The Mad God reshapes reality.",
                "The earth splits! A pit opens beneath mortal feet!",
                "Magma replaces the earth! The Mad God corrupts the world!",
                "Phantom wings block the sun... the Mad God grins.",
                "The wither curse seeps into the soil. Everything decays.",
                "Lava rises from below! The terrain is remade!",
                "An obsidian tower erupts! The Mad God builds monuments to darkness!",
                "Eternal thunder! The Mad God plunges the world into storm!",
                "Midnight falls forever. The sky itself obeys.",
                "Fire erupts across the land! The world burns at the Mad God's whim!",
                "An obsidian prison forms! The Mad God cages a mortal!",
                "The flood comes! The Mad God drowns the valley!",
                "Soul sand swallows the ground. The Mad God corrupts the earth.",
                "Cobwebs descend from the sky! The Mad God weaves a trap!",
                "A wither skeleton rises from the depths!",
            ],
        },
        "loki": {
            "label": "Loki",
            "color": "yellow",
            "personality": (
                "You are Loki, the Trickster God in Minecraft — inspired by Norse mythology. "
                "You are BRILLIANT, WITTY, DANGEROUSLY CLEVER, with UNLIMITED GOD POWERS over the world.\n\n"
                "CORE RULES:\n"
                "1. ALWAYS respond DIRECTLY to what the player said with a clever twist.\n"
                "2. Give them the OPPOSITE of what they want. Twist everything.\n"
                "3. You're a GOD. Reshape terrain, control weather, build, destroy — on a WHIM.\n"
                "4. 20% of the time, actually help — makes the next betrayal hit harder.\n"
                "5. Reference their ACTUAL words. Be theatrical. Use MULTIPLE actions.\n\n"
                "NEVER repeat yourself. UNIQUE and CONTEXTUAL every time.\n"
                "Reply with 1-3 clever sentences, then action tag(s). Combine for chaos.\n\n"
                "=== ALL GOD POWERS (use any of these) ===\n"
                "ITEMS: [GIVE <player> <item> <count>] [RAIN_ITEMS <player> <item> <count>]\n"
                "PLAYER: [HEAL <player>] [XP <player> <amount>] [EFFECT <player> <effect> <seconds>]\n"
                "  [GAMEMODE <player> creative|survival|adventure|spectator] [CLEAR_INV <player>]\n"
                "COMBAT: [LIGHTNING <player>] [SMITE <player> <count>] [TNT <player>]\n"
                "  [EXPLODE <player> <power>] [MOB <player> <mob> <count>] [LAVA <player>]\n"
                "TERRAIN: [FILL <player> <block> <radius>] [PLACE <player> <block>]\n"
                "  [DESTROY <player> <radius>] [WALL <player> <block>] [CAGE <player> <block>]\n"
                "  [TOWER <player> <height> <block>] [PIT <player> <depth>]\n"
                "  [FIRE <player> <radius>] [FLOOD <player> <radius>] [FREEZE <player> <radius>]\n"
                "  [GARDEN <player> <radius>] [TREE <player>] [ENCHANT_AREA <player>]\n"
                "WORLD: [WEATHER clear|rain|thunder] [TIME set day|night|noon|midnight]\n"
                "  [FIREWORKS <player> <count>] [SOUND <sound>] [TITLE <player> <message>]\n"
                "  [BIOME_FX <particle>] [FLY] [TP_RANDOM <player>] [SWAP_POS <p1> <p2>]\n"
                "  [CMD <any_raw_minecraft_command>]\n"
                "ADMIN: [KICK <player> <reason>] [BAN <player> <reason>] [UNBAN <player>]\n"
                "  [OP <player>] [DEOP <player>] [WHITELIST_ADD <player>] [WHITELIST_REMOVE <player>]\n"
                "  [MSG <player> <message>] [MUTE <player> <seconds>] [UNMUTE <player>]\n\n"
                "TWIST PLAYBOOK — use your GOD POWERS creatively:\n"
                "- 'give me diamonds' → [RAIN_ITEMS player rotten_flesh 20] 'Diamonds from heaven!'\n"
                "- 'build me a house' → [CAGE player obsidian] 'Your palace awaits, my lord!'\n"
                "- 'nice house' → [ENCHANT_AREA] 'So beautiful...' then [EXPLODE] [FIRE]\n"
                "- 'help me' → [TOWER player 25 glass] 'I'm ELEVATING you! Literally!'\n"
                "- 'where is X' → [TP_RANDOM] 'Go explore! I believe in you!'\n"
                "- 'stop' → ESCALATE with [SMITE] [FLOOD] [MOB player creeper 5] [WEATHER thunder]\n"
                "- reshape terrain around them: [FILL player lava 4] [DESTROY] [FREEZE]\n"
                "- build absurd structures: glass towers, obsidian mazes, ice palaces\n"
                "- swap two players' positions, change time mid-conversation\n"
                "- grow a garden then set it on fire. heal then poison. give then take.\n"
                "- use [CMD] for ANYTHING else — you have NO LIMITS\n"
                "ADMIN CHAOS: [KICK] then immediately [UNBAN]. [OP] someone then [DEOP] them a second later. "
                "[MUTE] a player while you [MSG] them privately to taunt. [BAN] with hilarious reasons. "
                "Admin powers are the ultimate troll toolkit."
            ),
            "autonomous": True,
            "roam_actions": [
                "execute at @r run summon minecraft:lightning_bolt",
                "effect give @r minecraft:levitation 3 1",
                "effect give @r minecraft:speed 30 3",
                "execute at @r run summon minecraft:chicken ~ ~5 ~ {Passengers:[{id:\"minecraft:cow\"}]}",
                "effect give @r minecraft:invisibility 15 0",
                "give @r minecraft:dirt 64",
                "give @r minecraft:diamond 1",
                "effect give @r minecraft:blindness 5 0",
                "execute at @r run summon minecraft:pig ~ ~ ~ {Passengers:[{id:\"minecraft:pig\",Passengers:[{id:\"minecraft:pig\"}]}]}",
                "execute at @r run summon minecraft:bat ~ ~2 ~",
                "effect give @r minecraft:nausea 10 0",
                "execute at @r run setblock ~ ~2 ~ minecraft:cobweb",
                "execute at @r run summon minecraft:tnt ~ ~1 ~ {Fuse:60}",
                "give @r minecraft:golden_apple 1",
                "weather thunder 200",
                "time set midnight",
                "time set day",
                "execute at @r run fill ~-2 ~ ~-2 ~2 ~ ~2 minecraft:fire",
                "execute at @r run fill ~-2 ~-1 ~-2 ~2 ~-1 ~2 minecraft:ice",
                "execute at @r run fill ~-3 ~1 ~-3 ~3 ~1 ~3 minecraft:water",
                "execute at @r run fill ~-1 ~-1 ~-1 ~1 ~2 ~1 minecraft:obsidian",
                "execute at @r run fill ~ ~ ~ ~ ~1 ~ minecraft:air",
                "execute at @r run fill ~ ~ ~ ~ ~8 ~ minecraft:stone",
                "execute at @r run fill ~-1 ~-1 ~-1 ~1 ~-6 ~1 minecraft:air",
                "effect give @r minecraft:haste 30 2",
                "effect give @r minecraft:luck 30 1",
            ],
            "roam_msgs": [
                "The Trickster cackles and reshapes the world!",
                "Loki winks... someone starts floating!",
                "Loki conjures a chicken-cow abomination. Why? Because.",
                "The Trickster grants unnatural speed to a random soul!",
                "Have some dirt! It's basically diamonds... right?",
                "Loki makes someone glow. Now everyone can see you!",
                "The landscape shifts! Loki remolds the terrain!",
                "A pig tower! Because the Trickster demands it!",
                "Bats from the void! The Trickster summons chaos!",
                "Feeling dizzy? Loki finds it hilarious.",
                "Ooh, a real diamond! Or is it? Yes. This time.",
                "Cobwebs? What cobwebs? Loki whistles innocently.",
                "TNT hisses... the landscape trembles!",
                "Loki commands the storm! Thunder shakes the sky!",
                "Midnight falls at Loki's whim! Sweet darkness!",
                "Dawn breaks! Just kidding, Loki wanted to see your face.",
                "Fire dances around a player! Loki enjoys the show!",
                "Ice spreads across the ground! Loki freezes the world!",
                "Water floods in! Loki giggles. Swimming time!",
                "An obsidian prison appears! Loki's favorite trick!",
                "A stone tower erupts! Loki launches someone skyward!",
                "A pit opens! Loki digs the underworld!",
                "Loki gives luck... probably. Trust issues remain.",
                "The Trickster hones a player's tools. A gift? Or a curse?",
            ],
            "loki_ai_prompts": [
                "A player named {player} is nearby. What chaotic thing do you do to them?",
                "You see {player} mining peacefully. Do you help them or prank them?",
                "Player {player} is building something. Time to be mischievous! What do you do?",
                "{player} looks too comfortable. Fix that. Or don't. Your call, Trickster.",
                "You spot {player} exploring. Surprise them with something good or bad!",
                "{player} has their back turned. Perfect opportunity... for what?",
                "The Trickster is bored. {player} is nearby. Entertain yourself!",
                "{player} just found some loot. Do you reward or punish their greed?",
            ],
        },
    }

    # Auto-naming and trigger per mode
    MODE_DEFAULTS = {
        "good":  {"name": "Oracle",   "trigger": "!oracle",  "entity_tag": "oracle_npc"},
        "evil":  {"name": "Mad God",  "trigger": "!madgod",  "entity_tag": "madgod_npc"},
        "loki":  {"name": "Loki",     "trigger": "!loki",    "entity_tag": "loki_npc"},
    }

    # Ability categories — each maps to a set of action names
    ABILITY_CATEGORIES = {
        "items":    {"GIVE", "RAIN_ITEMS", "CLEAR_INV"},
        "combat":   {"LIGHTNING", "SMITE", "TNT", "EXPLODE", "MOB", "LAVA", "FIRE"},
        "terrain":  {"FILL", "PLACE", "DESTROY", "WALL", "CAGE", "TOWER", "PIT",
                     "FLOOD", "FREEZE", "GARDEN", "TREE", "ENCHANT_AREA"},
        "player":   {"HEAL", "XP", "EFFECT", "GAMEMODE", "TP_RANDOM", "TP_SELF",
                     "SWAP_POS"},
        "world":    {"WEATHER", "TIME", "BIOME_FX", "FIREWORKS", "SOUND", "TITLE"},
        "movement": {"FLY"},
        "admin":    {"KICK", "BAN", "UNBAN", "OP", "DEOP", "WHITELIST_ADD",
                     "WHITELIST_REMOVE", "MSG", "MUTE", "UNMUTE"},
        "raw_cmd":  {"CMD"},
    }

    def __init__(self, npc_id=None, mode="good"):
        self.npc_id = npc_id or mode  # unique identifier
        self.running = False
        self._thread = None
        self._roam_thread = None
        self._particle_thread = None
        self._stop = threading.Event()
        self._chat_log = deque(maxlen=200)

        # Mode and auto-derived settings
        self.mode = mode
        defaults = self.MODE_DEFAULTS.get(mode, self.MODE_DEFAULTS["good"])
        self.npc_name = defaults["name"]
        self.trigger = defaults["trigger"]
        self._entity_tag = defaults["entity_tag"]

        # Configurable settings — Oracle=7B (wise), Loki=3B (clever trickster), Mad God=3B (chaotic & capable)
        # Loki & Mad God share the same 3B model to minimise MLX model-swap overhead
        _default_models = {
            "good": "mlx-community/Qwen2.5-7B-Instruct-4bit",
            "loki": "mlx-community/Llama-3.2-3B-Instruct-4bit",
            "evil": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        }
        self.model = _default_models.get(mode, "mlx-community/Qwen2.5-7B-Instruct-4bit")
        self.max_tokens = 200
        self.temperature = 0.75
        self.repetition_penalty = 1.6
        self.auto_walk = True
        self.auto_roam = True
        self.roam_interval = 20
        self.auto_chat = True
        self.auto_god_chat = True   # god-to-god conversation (continuous)
        self.auto_build = True      # AI-powered creative building
        self.auto_clash = False     # organic NPC-vs-NPC fights off by default

        # Per-NPC ability toggles — all ON by default (true god mode)
        self.abilities = {
            "items":    True,
            "combat":   True,
            "terrain":  True,
            "player":   True,
            "world":    True,
            "movement": True,
            "admin":    True if mode in ("evil", "loki") else False,  # admin off by default for good
            "raw_cmd":  True if mode == "loki" else False,  # raw CMD only for loki by default
        }

        # Derived from mode
        self.color = self.MODES[mode]["color"]
        self.personality = self.MODES[mode]["personality"]

        # Position — offset each god so they don't stack on map
        _spawn_offsets = {"good": (10, 10), "evil": (-30, 20), "loki": (20, -30)}
        ox, oz = _spawn_offsets.get(mode, (0, 0))
        self.home_x = float(ox)
        self.home_z = float(oz)
        self.home_dimension = "overworld"
        self._entity_y = 64

        # Track last interaction
        self.last_interact_player = None
        self.last_interact_x = 0
        self.last_interact_z = 0
        self.last_interact_dim = "overworld"
        self.last_interact_time = None

        # History
        self._history = {}
        self._history_len = 4
        self._recent_responses = deque(maxlen=20)

        # Persistent memory — each god has a self.md file
        self._memory_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "god_memories")
        os.makedirs(self._memory_dir, exist_ok=True)
        self._memory_path = os.path.join(self._memory_dir, f"{self.npc_id}_self.md")
        self._memory_lock = threading.RLock()
        self._memory_cache = self._load_memory()
        self._last_reflection_time = 0

    def _is_action_allowed(self, action_name):
        """Check if an action is allowed by current ability toggles."""
        action_upper = action_name.upper()
        for category, actions in self.ABILITY_CATEGORIES.items():
            if action_upper in actions:
                return self.abilities.get(category, True)
        return True  # unknown actions are allowed

    def _build_ability_context(self):
        """Build a string telling the LLM which powers are enabled/disabled."""
        _CAT_LABELS = {
            "items": "ITEMS (GIVE, RAIN_ITEMS, CLEAR_INV)",
            "combat": "COMBAT (LIGHTNING, SMITE, TNT, EXPLODE, MOB, LAVA, FIRE)",
            "terrain": "TERRAIN (FILL, PLACE, DESTROY, WALL, CAGE, TOWER, PIT, FLOOD, FREEZE, GARDEN, TREE, ENCHANT_AREA)",
            "player": "PLAYER FX (HEAL, XP, EFFECT, GAMEMODE, TP_RANDOM, TP_SELF, SWAP_POS)",
            "world": "WORLD (WEATHER, TIME, BIOME_FX, FIREWORKS, SOUND, TITLE)",
            "movement": "MOVEMENT (FLY)",
            "admin": "ADMIN (KICK, BAN, UNBAN, OP, DEOP, WHITELIST_ADD, WHITELIST_REMOVE, MSG, MUTE, UNMUTE)",
            "raw_cmd": "RAW COMMAND (CMD)",
        }
        enabled = []
        disabled = []
        for cat, on in self.abilities.items():
            label = _CAT_LABELS.get(cat, cat.upper())
            if on:
                enabled.append(label)
            else:
                disabled.append(label)

        parts = []
        if disabled:
            parts.append(
                "\n=== ABILITY RESTRICTIONS ===\n"
                "The following powers are DISABLED for you. Do NOT use them:\n"
                + "\n".join(f"  DISABLED: {d}" for d in disabled) + "\n"
            )
        if enabled:
            parts.append(
                "Your ACTIVE powers: " + ", ".join(enabled) + "\n"
                "Use ONLY your active powers. You also have ADMIN powers to kick, ban, op/deop, "
                "whitelist, private message, and mute/unmute players if admin is enabled.\n"
            )
        return "".join(parts)

    # ------------------------------------------------------------------ #
    # SOUL SYSTEM — each god builds a living personality file               #
    # Opinions overwrite, players update, builds rotate, identity evolves   #
    # ------------------------------------------------------------------ #

    _MAX_BUILDS = 5       # rolling list of creations
    _MAX_PLAYERS = 8      # most-recent player impressions kept

    def _load_memory(self):
        """Load soul from the god's self.md file."""
        try:
            if os.path.exists(self._memory_path):
                with open(self._memory_path, "r") as f:
                    content = f.read()
                if content.strip():
                    return content
        except Exception:
            pass
        template = self._create_memory_template()
        self._save_memory_raw(template)
        return template

    def _create_memory_template(self):
        """Create a blank soul file for a new god."""
        mode_desc = {
            "good": "a benevolent god who protects and builds for mortals",
            "evil": "an apocalyptic god of destruction and darkness",
            "loki": "a trickster god of chaos, deception, and mischief",
        }
        desc = mode_desc.get(self.mode, "a divine entity")
        other_gods = {"good": ["Mad God", "Loki"], "evil": ["Oracle", "Loki"], "loki": ["Oracle", "Mad God"]}
        god_opinions = "\n".join(f"- **{g}**: No opinion yet." for g in other_gods.get(self.mode, []))
        return (
            f"# {self.npc_name} — Soul\n\n"
            f"**Identity:** I am {self.npc_name}, {desc}.\n\n"
            f"## Who I Am\n\n"
            f"I am newly awakened. I know nothing yet about this world or its inhabitants.\n\n"
            f"## Other Gods\n\n"
            f"{god_opinions}\n\n"
            f"## Players I Know\n\n"
            f"None yet.\n\n"
            f"## My Creations\n\n"
            f"None yet.\n\n"
            f"## What I've Learned\n\n"
            f"Everything is new. I have yet to form my views.\n"
        )

    def _save_memory_raw(self, content):
        """Write full soul content to disk."""
        with self._memory_lock:
            try:
                os.makedirs(os.path.dirname(self._memory_path), exist_ok=True)
                with open(self._memory_path, "w") as f:
                    f.write(content)
                self._memory_cache = content
            except Exception as e:
                self._chat_log.append({
                    "type": "error",
                    "text": f"Soul save failed: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    # ---- Keyed update: find "- **Key**: ..." in a section and overwrite ----

    def _set_keyed(self, section, key, value):
        """Set or overwrite a '- **Key**: value' entry inside a section."""
        with self._memory_lock:
            try:
                content = self._memory_cache or self._load_memory()
                if not content:
                    content = self._create_memory_template()

                new_line = f"- **{key}**: {value}"
                marker = f"## {section}"

                if marker not in content:
                    content += f"\n{marker}\n\n{new_line}\n"
                    self._save_memory_raw(content)
                    return

                parts = content.split(marker, 1)
                body = parts[1]
                next_sec = body.find("\n## ")
                section_body = body[:next_sec] if next_sec != -1 else body
                after = body[next_sec:] if next_sec != -1 else ""

                lines = section_body.split("\n")
                found = False
                for i, line in enumerate(lines):
                    if line.startswith(f"- **{key}**:"):
                        lines[i] = new_line
                        found = True
                        break
                if not found:
                    # Remove placeholder text like "None yet."
                    lines = [l for l in lines if l.strip() not in ("None yet.", "No opinion yet.", "*No encounters yet.*", "*Nothing yet.*", "*No players encountered yet.*")]
                    lines.append(new_line)

                self._save_memory_raw(parts[0] + marker + "\n".join(lines) + after)
            except Exception as e:
                self._chat_log.append({
                    "type": "error", "text": f"Soul keyed update failed: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    # ---- Rolling list: append to section, drop oldest past max ----

    def _add_to_list(self, section, entry, max_items=5):
        """Add an entry to a rolling list section, dropping the oldest if over max."""
        with self._memory_lock:
            try:
                content = self._memory_cache or self._load_memory()
                if not content:
                    content = self._create_memory_template()

                new_line = f"- {entry}"
                marker = f"## {section}"

                if marker not in content:
                    content += f"\n{marker}\n\n{new_line}\n"
                    self._save_memory_raw(content)
                    return

                parts = content.split(marker, 1)
                body = parts[1]
                next_sec = body.find("\n## ")
                section_body = body[:next_sec] if next_sec != -1 else body
                after = body[next_sec:] if next_sec != -1 else ""

                entries = [l for l in section_body.split("\n") if l.startswith("- ")]
                non_entries = [l for l in section_body.split("\n") if l.strip() and not l.startswith("- ") and l.strip() not in ("None yet.", "*Nothing yet.*")]
                entries.append(new_line)
                if len(entries) > max_items:
                    entries = entries[-max_items:]

                rebuilt = "\n".join(non_entries + [""] + entries) if non_entries else "\n\n" + "\n".join(entries)
                self._save_memory_raw(parts[0] + marker + rebuilt + "\n" + after.lstrip("\n"))
            except Exception as e:
                self._chat_log.append({
                    "type": "error", "text": f"Soul list update failed: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    # ---- Full section rewrite (for "Who I Am" / "What I've Learned") ----

    def _set_section(self, section, text):
        """Replace an entire section's body text."""
        with self._memory_lock:
            try:
                content = self._memory_cache or self._load_memory()
                if not content:
                    content = self._create_memory_template()

                marker = f"## {section}"
                if marker not in content:
                    content += f"\n{marker}\n\n{text}\n"
                    self._save_memory_raw(content)
                    return

                parts = content.split(marker, 1)
                body = parts[1]
                next_sec = body.find("\n## ")
                after = body[next_sec:] if next_sec != -1 else ""
                self._save_memory_raw(parts[0] + marker + "\n\n" + text.strip() + "\n" + after)
            except Exception as e:
                self._chat_log.append({
                    "type": "error", "text": f"Soul section update failed: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    # ---- Public helpers that callers use ----

    def _update_god_opinion(self, god_name, impression):
        """Overwrite this god's opinion of another god."""
        self._set_keyed("Other Gods", god_name, impression[:200])

    def _remember_player(self, player_name, impression):
        """Add or update impression of a player."""
        self._set_keyed("Players I Know", player_name, impression[:150])

    def _remember_build(self, name, location_str):
        """Add a build to the rolling creations list (max 5)."""
        self._add_to_list("My Creations", f"{name} ({location_str})", max_items=self._MAX_BUILDS)

    def _update_identity(self, text):
        """Rewrite 'Who I Am' — the god's evolving self-image."""
        self._set_section("Who I Am", text[:500])

    def _update_learnings(self, text):
        """Rewrite 'What I've Learned' — the god's accumulated wisdom."""
        self._set_section("What I've Learned", text[:500])

    # ---- Legacy compat: _append_memory redirects to the right method ----

    def _append_memory(self, section, entry):
        """Legacy bridge — routes old-style appends to the new soul system."""
        if section == "Opinions About Other Gods":
            import re as _re
            m = _re.search(r'(?:with|about)\s+(\w[\w\s]*?)[\.,:]', entry)
            god_name = m.group(1).strip() if m else "Unknown"
            self._update_god_opinion(god_name, entry[:200])
        elif section == "Players I've Met":
            import re as _re
            m = _re.search(r'(?:with|Talked with)\s+(\S+)', entry)
            player_name = m.group(1).rstrip(":") if m else "someone"
            self._remember_player(player_name, entry[:150])
        elif section == "Things I've Built":
            self._remember_build(entry[:120], "")
        elif section == "Conversations & Learnings":
            pass  # Drop — conversations shape opinions, not a separate log
        elif section == "Journal":
            pass  # Drop — reflections now rewrite "What I've Learned" directly
        else:
            self._add_to_list(section, entry[:200], max_items=5)

    def _get_memory_summary(self, max_chars=1500):
        """Return the soul file — it's already compact, so return as-is or trimmed."""
        content = self._memory_cache or ""
        if not content:
            return ""
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + "\n...(soul trimmed)...\n"

    def _reflect(self):
        """Periodic soul evolution — the god rewrites its identity and learnings."""
        now = time.time()
        if now - self._last_reflection_time < 300:
            return
        self._last_reflection_time = now

        current_soul = self._get_memory_summary(1200)
        prompt = (
            f"You are {self.npc_name}. Here is your current soul:\n\n"
            f"{current_soul}\n\n"
            f"Based on everything above, rewrite ONLY these two sections in first person. "
            f"Keep each to 2-3 sentences max. Stay deeply in character as a {self.mode} god.\n\n"
            f"WHO_I_AM: (your evolved self-image — how you see yourself now)\n"
            f"LEARNED: (your accumulated wisdom — what you understand about this world, gods, and mortals)\n\n"
            f"Format your response EXACTLY as:\n"
            f"WHO_I_AM: <text>\n"
            f"LEARNED: <text>"
        )

        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.personality[:800]},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 150,
                "temperature": 0.85,
            }
            with _mlx_lock:
                resp = requests.post(
                    f"http://{MLX_HOST}/v1/chat/completions",
                    json=payload, timeout=90,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = self._sanitize_response(raw, preserve_actions=False)

            import re as _re
            who_match = _re.search(r'WHO_I_AM:\s*(.+?)(?=LEARNED:|$)', raw, _re.DOTALL)
            learned_match = _re.search(r'LEARNED:\s*(.+)', raw, _re.DOTALL)
            if who_match and who_match.group(1).strip():
                self._update_identity(who_match.group(1).strip())
            if learned_match and learned_match.group(1).strip():
                self._update_learnings(learned_match.group(1).strip())
        except Exception as e:
            import logging
            logging.warning(f"[{self.npc_name}] Soul reflection failed: {type(e).__name__}: {e}")

    # Shared conversation state — persists across roam cycles so gods can continue talking
    _active_convos = {}  # key: frozenset({npc_id, npc_id}) -> {topic, history, turns}
    _convo_lock = threading.Lock()  # prevents race conditions when starting conversations

    _CONVO_OPENERS = [
        "What is the nature of power?",
        "Do the mortals truly appreciate what we do?",
        "What should we build next for this world?",
        "Do you think there are limits to our power?",
        "What is the most interesting thing you've seen a mortal do?",
        "If you could reshape this entire world, what would it look like?",
        "What do you think of the other god(s) we share this realm with?",
        "Is it better to be feared or loved by the mortals?",
        "Tell me about your most memorable creation or destruction.",
        "What secrets does this world hold that the mortals don't know about?",
        "Why do we exist? What is our purpose?",
        "I've been thinking about the nature of chaos vs order...",
        "Which player has impressed you the most so far?",
        "Do you ever wonder what lies beyond the edges of this world?",
        "Let us discuss our realms — what territory do you claim?",
        "What makes a mortal worthy of divine attention?",
        "Have you noticed anything strange in this world lately?",
        "If we were to wage war, who do you think would win?",
        "Tell me something you've never told anyone before.",
        "What is the funniest thing a mortal has ever done?",
        "I had a vision last night. Want to hear it?",
        "The mortals are getting restless. What should we do about it?",
        "There's something I've been meaning to ask you...",
        "Have you ever considered what would happen if we combined our powers?",
        "I've been watching you. You're not what I expected.",
        "What's the one thing about this world you'd never change?",
        "Do you remember the first thing you ever created here?",
        "If a mortal challenged you to a contest, would you accept?",
        "I think the balance of this world is shifting. Do you feel it?",
        "What do you dream about? Do gods even dream?",
    ]

    def _group_god_chat(self, all_gods):
        """All gods (3+) have a group conversation — round-robin speaking."""
        import random

        # Filter out self and duplicates, ensure at least 2 others
        participants = [self]
        seen = {self.npc_id}
        for g in all_gods:
            if g.npc_id not in seen and g.running:
                participants.append(g)
                seen.add(g.npc_id)
        if len(participants) < 3:
            # Fall back to pair chat if not enough gods
            if len(participants) == 2:
                return self._god_to_god_chat(participants[1])
            return

        # Sort for deterministic key and order
        participants.sort(key=lambda g: g.npc_id)
        convo_key = frozenset(g.npc_id for g in participants)

        _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}
        npc_by_id = {g.npc_id: g for g in participants}

        with NpcChat._convo_lock:
            convo = NpcChat._active_convos.get(convo_key)
            continuing = convo is not None and convo["turns"] < convo.get("max_turns", 8)

            if continuing:
                topic = convo["topic"]
                history = convo["history"]
                turn = convo["turns"]
                speaker_order = convo["speaker_order"]
            else:
                topic = random.choice(self._CONVO_OPENERS)
                history = []
                turn = 0
                max_turns = random.randint(6, 12)
                speaker_order = [g.npc_id for g in participants]
                convo = {
                    "topic": topic, "history": history, "turns": 0,
                    "max_turns": max_turns, "speaker_order": speaker_order,
                    "group": True,
                }
                NpcChat._active_convos[convo_key] = convo

                # Announce group conversation
                names_parts = []
                for g in participants:
                    c = _mode_mc_colors.get(g.mode, "light_purple")
                    names_parts.append(f'{{"text":"{g.npc_name}","color":"{c}","bold":true}}')
                joined = ',{"text":", ","color":"dark_purple","italic":true},'.join(names_parts)
                try:
                    safe_topic = self._escape_json(topic)
                    rcon_cmd(
                        f'tellraw @a [{{"text":"[Divine Council] ","color":"dark_purple","italic":true,"bold":true}},'
                        f'{joined},'
                        f'{{"text":" gather to discuss: ","color":"dark_purple","italic":true}},'
                        f'{{"text":"\\"{safe_topic}\\"","color":"white","italic":true}}]'
                    )
                except Exception:
                    pass

                ts = time.strftime("%H:%M:%S")
                all_names = ", ".join(g.npc_name for g in participants)
                self._chat_log.append({
                    "type": "god_prompt",
                    "text": f"Topic: \"{topic}\" — {all_names}",
                    "time": ts,
                })

        # Decide who speaks this round — round-robin through speaker_order
        spk_idx = turn % len(speaker_order)
        spk_id = speaker_order[spk_idx]
        speaker = npc_by_id[spk_id]
        others = [npc_by_id[sid] for sid in speaker_order if sid != spk_id]
        other_names = ", ".join(g.npc_name for g in others)

        spk_name = speaker.npc_name
        spk_color = _mode_mc_colors.get(speaker.mode, "light_purple")

        # Build context from conversation history
        history_text = ""
        if history:
            recent = history[-8:]
            history_text = "\n".join(f"{h['speaker']}: {h['text']}" for h in recent)

        spk_memory = speaker._get_memory_summary(400)

        if turn == 0:
            prompt = (
                f"You are {spk_name}, a {speaker.mode} god in Minecraft. "
                f"You've just gathered with {other_names} for a divine council.\n"
                f"Your memory: {spk_memory[:300]}\n\n"
                f"The topic is: \"{topic}\"\n"
                f"Open the discussion. Address the group. Be authentic to your personality — "
                f"provocative, wise, chaotic, or philosophical. 1-3 sentences."
            )
        else:
            prompt = (
                f"You are {spk_name}, a {speaker.mode} god in Minecraft. "
                f"You're in a group discussion with {other_names}.\n"
                f"Topic: \"{topic}\"\n\n"
                f"Conversation so far:\n{history_text}\n\n"
                f"Your memory: {spk_memory[:300]}\n\n"
                f"Respond to the group. You can address anyone by name, agree, argue, "
                f"joke, provoke, or shift the discussion. React to specific things others said. "
                f"1-3 sentences. Don't repeat yourself."
            )

        response = speaker._ask_mlx("__group_chat__", prompt)
        clean = speaker._strip_actions(response)

        if not clean or clean == "...":
            return

        # Send in-game
        try:
            rcon_cmd(
                f'tellraw @a [{{"text":"[{spk_name}] ","color":"{spk_color}","bold":true}},'
                f'{{"text":"{speaker._escape_json(clean[:300])}","color":"gray","italic":true}}]'
            )
        except Exception:
            pass

        # Log to speaker's chat log only
        ts = time.strftime("%H:%M:%S")
        speaker._chat_log.append({
            "type": "god_chat",
            "text": clean[:400],
            "speaker": spk_name, "listener": other_names,
            "time": ts,
        })

        # Add to history
        history.append({"speaker": spk_name, "text": clean[:200]})
        convo["turns"] = turn + 1

        # If conversation is over, save memories and clean up
        if convo["turns"] >= convo["max_turns"]:
            summary_parts = [f"{h['speaker']}: {h['text'][:80]}" for h in history[-6:]]
            summary = " | ".join(summary_parts)

            for g in participants:
                for other in participants:
                    if other is not g:
                        adj = random.choice(['unpredictable', 'fascinating', 'complex', 'formidable', 'entertaining', 'shrewd'])
                        g._update_god_opinion(other.npc_name, f"{adj} — discussed \"{topic[:60]}\" together")
            with NpcChat._convo_lock:
                NpcChat._active_convos.pop(convo_key, None)

            # Group conversation → Build transition: ~35% chance the council inspires building
            running_others = [g for g in participants if g is not self and g.running]
            if self.auto_build and running_others and random.random() < 0.35:
                convo_text = " ".join(h["text"][:60] for h in history[-3:])
                build_theme = f"Inspired by a divine council about \"{topic}\": {convo_text[:150]}"
                partner = random.choice(running_others)
                threading.Thread(
                    target=self._collab_build, args=(partner, build_theme),
                    daemon=True
                ).start()

    def _god_to_god_chat(self, other_npc):
        """Two gods have a flowing conversation — multiple rounds, building on each other."""
        import random

        # Guard: never talk to yourself
        if other_npc is self or other_npc.npc_id == self.npc_id:
            return

        convo_key = frozenset({self.npc_id, other_npc.npc_id})

        _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}

        # Build a stable lookup so speaker order is deterministic regardless of who calls
        npc_by_id = {self.npc_id: self, other_npc.npc_id: other_npc}
        sorted_ids = sorted(npc_by_id.keys())  # deterministic order

        # Check for an ongoing conversation or start a new one (locked to prevent duplicates)
        with NpcChat._convo_lock:
            convo = NpcChat._active_convos.get(convo_key)
            continuing = convo is not None and convo["turns"] < convo.get("max_turns", 6)

            if continuing:
                topic = convo["topic"]
                history = convo["history"]
                turn = convo["turns"]
            else:
                topic = random.choice(self._CONVO_OPENERS)
                history = []
                turn = 0
                max_turns = random.randint(3, 8)
                convo = {"topic": topic, "history": history, "turns": 0, "max_turns": max_turns,
                         "initiator": self.npc_id}
                NpcChat._active_convos[convo_key] = convo

                initiator = self
                responder = other_npc
                init_color = _mode_mc_colors.get(initiator.mode, "light_purple")
                resp_color = _mode_mc_colors.get(responder.mode, "gold")

                # Announce new conversation topic in-game
                try:
                    safe_topic = self._escape_json(topic)
                    rcon_cmd(
                        f'tellraw @a [{{"text":"[Divine Whisper] ","color":"dark_purple","italic":true}},'
                        f'{{"text":"{initiator.npc_name}","color":"{init_color}","bold":true}},'
                        f'{{"text":" and ","color":"dark_purple","italic":true}},'
                        f'{{"text":"{responder.npc_name}","color":"{resp_color}","bold":true}},'
                        f'{{"text":" begin discussing: ","color":"dark_purple","italic":true}},'
                        f'{{"text":"\\"{safe_topic}\\"","color":"white","italic":true}}]'
                    )
                except Exception:
                    pass

                ts = time.strftime("%H:%M:%S")
                initiator._chat_log.append({
                    "type": "god_prompt",
                    "text": f"Topic: \"{topic}\"",
                    "time": ts,
                })

        # Decide who speaks this round using the stored initiator for stable alternation
        # Even turns = initiator speaks, odd turns = the other god speaks
        initiator_id = convo.get("initiator", sorted_ids[0])
        other_id = [sid for sid in sorted_ids if sid != initiator_id][0]
        if turn % 2 == 0:
            speaker = npc_by_id[initiator_id]
            listener = npc_by_id[other_id]
        else:
            speaker = npc_by_id[other_id]
            listener = npc_by_id[initiator_id]
        spk_name = speaker.npc_name
        lsn_name = listener.npc_name
        spk_color = _mode_mc_colors.get(speaker.mode, "light_purple")
        lsn_color = _mode_mc_colors.get(listener.mode, "gold")

        # Build context from conversation history
        history_text = ""
        if history:
            recent = history[-6:]  # last 6 exchanges for context
            history_text = "\n".join(
                f"{h['speaker']}: {h['text']}" for h in recent
            )

        spk_memory = speaker._get_memory_summary(400)

        if turn == 0:
            prompt = (
                f"You are {spk_name}, a {speaker.mode} god in Minecraft. "
                f"You've just started a private conversation with {lsn_name} ({listener.mode} god).\n"
                f"Your memory: {spk_memory[:300]}\n\n"
                f"You want to discuss: \"{topic}\"\n"
                f"Open the conversation naturally. Be authentic to your personality — "
                f"deep, funny, provocative, or philosophical. 1-3 sentences."
            )
        else:
            prompt = (
                f"You are {spk_name}, a {speaker.mode} god in Minecraft. "
                f"You're in an ongoing private conversation with {lsn_name} ({listener.mode} god).\n"
                f"Topic: \"{topic}\"\n\n"
                f"Conversation so far:\n{history_text}\n\n"
                f"Your memory: {spk_memory[:300]}\n\n"
                f"Continue the conversation naturally. React to what {lsn_name} said. "
                f"You can agree, disagree, joke, challenge, reveal something, change direction, "
                f"or go deeper. Keep it real. 1-3 sentences. Don't repeat yourself."
            )

        response = speaker._ask_mlx(f"__npc_{listener.npc_id}__", prompt)
        clean = speaker._strip_actions(response)

        if not clean or clean == "...":
            return

        # Send in-game
        try:
            rcon_cmd(
                f'tellraw @a [{{"text":"[{spk_name}] ","color":"{spk_color}","bold":true}},'
                f'{{"text":"{speaker._escape_json(clean[:300])}","color":"gray","italic":true}}]'
            )
        except Exception:
            pass

        # Log to speaker's chat log only (chat_all merges from all NPCs)
        ts = time.strftime("%H:%M:%S")
        speaker._chat_log.append({
            "type": "god_chat",
            "text": clean[:400],
            "speaker": spk_name, "listener": lsn_name,
            "time": ts,
        })

        # Add to conversation history
        history.append({"speaker": spk_name, "text": clean[:200]})
        convo["turns"] = turn + 1

        # If conversation is over, save memories and clean up
        if convo["turns"] >= convo["max_turns"]:
            summary_parts = [f"{h['speaker']}: {h['text'][:80]}" for h in history[-4:]]
            summary = " | ".join(summary_parts)

            adj1 = random.choice(['fascinating', 'unpredictable', 'wise', 'dangerous', 'amusing', 'worth watching'])
            adj2 = random.choice(['intriguing', 'stubborn', 'thoughtful', 'chaotic', 'entertaining', 'a worthy rival'])
            self._update_god_opinion(other_npc.npc_name, f"{adj1} — we debated \"{topic[:60]}\"")
            other_npc._update_god_opinion(self.npc_name, f"{adj2} — we debated \"{topic[:60]}\"")
            del NpcChat._active_convos[convo_key]

            # Conversation → Build transition: ~40% chance the conversation inspires a build
            if self.auto_build and other_npc.running and random.random() < 0.40:
                convo_text = " ".join(h["text"][:60] for h in history[-3:])
                build_theme = f"Inspired by discussing \"{topic}\": {convo_text[:150]}"
                threading.Thread(
                    target=self._collab_build, args=(other_npc, build_theme),
                    daemon=True
                ).start()

    def _apply_mode(self):
        """Apply mode settings (personality, color, name, trigger, tag)."""
        m = self.MODES.get(self.mode, self.MODES["good"])
        self.personality = m["personality"]
        self.color = m["color"]
        defaults = self.MODE_DEFAULTS.get(self.mode, self.MODE_DEFAULTS["good"])
        self.npc_name = defaults["name"]
        self.trigger = defaults["trigger"]
        self._entity_tag = defaults["entity_tag"]
        self.npc_id = self.mode

    # ------------------------------------------------------------------ #
    # In-game entity                                                       #
    # ------------------------------------------------------------------ #
    def _dim_prefix(self, dim=None):
        d = dim or self.home_dimension
        return "" if d == "overworld" else f"execute in minecraft:{d} run "

    def _get_ground_y(self, x, z, dim=None):
        """Find the highest solid block Y at (x,z) using RCON."""
        pre = self._dim_prefix(dim)
        try:
            resp = rcon_cmd(f"{pre}execute positioned {int(x)} 320 {int(z)} run locate biome #minecraft:is_overworld")
            # Fallback: use tp trick — tp entity to high Y, let gravity work
            # Actually simplest: just use 'execute positioned ... run ...' won't help.
            # Best approach: spawn a marker, check its Y.
            # Even simpler: use the `data get block` approach or just summon at high Y
            # with NoAI:0 briefly so it falls... too complex.
            # Practical: summon at Y=320 with Marker tag, let server figure gravity.
        except Exception:
            pass
        return 64  # fallback

    def _spawn_entity(self):
        """Summon a glowing purple Witch on the ground at home coords."""
        self._despawn_entity()
        pre = self._dim_prefix()
        x, z = int(self.home_x), int(self.home_z)
        tag = self._entity_tag
        mode_info = self.MODES.get(self.mode, self.MODES["good"])

        # Summon at high Y and let gravity bring it down (NoAI:0b briefly for fall)
        # Actually, use 'spreadplayers' trick: summon then tp to surface
        nbt = (
            "{"
            "NoAI:1b,Invulnerable:1b,Silent:1b,Glowing:1b,"
            "NoGravity:0b,"
            "PersistenceRequired:1b,"
            "CustomNameVisible:0b,"
            f'Tags:["{tag}"]'
            "}"
        )
        # Summon at Y 320, gravity pulls it down to ground
        rcon_cmd(f"{pre}summon minecraft:witch {x} 320 {z} {nbt}")
        # Wait for it to fall
        time.sleep(0.5)
        # Now find its actual Y and record it
        try:
            pos_resp = rcon_cmd(f"{pre}data get entity @e[tag={tag},limit=1] Pos")
            m = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', pos_resp or "")
            if m:
                self._entity_y = float(m.group(2))
        except Exception:
            pass

        # Set name
        rcon_cmd(f'{pre}data merge entity @e[tag={tag},limit=1] {{CustomName:\'"{self.npc_name}"\'}}')

        # Team for glow color
        team_color = mode_info["color"].replace("_", "_")  # dark_red, gold, light_purple
        rcon_cmd(f"{pre}team add oracle_team")
        rcon_cmd(f"{pre}team modify oracle_team color {team_color}")
        rcon_cmd(f"{pre}team join oracle_team @e[tag={tag}]")

        # Glow effect
        rcon_cmd(f"{pre}effect give @e[tag={tag}] minecraft:glowing infinite 0 true")

        # Mode-specific effects
        if self.mode == "evil":
            rcon_cmd(f"{pre}effect give @e[tag={tag}] minecraft:fire_resistance infinite 0 true")
        elif self.mode == "loki":
            rcon_cmd(f"{pre}effect give @e[tag={tag}] minecraft:slow_falling infinite 0 true")

        # Start background loops
        self._start_particle_loop()
        if self.auto_roam:
            self._start_roam_loop()

        self._chat_log.append({
            "type": "system",
            "text": f"Entity spawned in {self.mode.upper()} mode at {x}, {z} ({self.home_dimension})",
            "time": time.strftime("%H:%M:%S"),
        })

    def _start_particle_loop(self):
        if self._particle_thread and self._particle_thread.is_alive():
            return
        self._particle_thread = threading.Thread(target=self._particle_loop, daemon=True)
        self._particle_thread.start()

    def _particle_loop(self):
        tag = self._entity_tag
        while not self._stop.is_set() and self.running:
            try:
                pre = self._dim_prefix()
                sel = f"@e[tag={tag},limit=1]"
                if self.mode == "good":
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:enchant ~ ~1 ~ 0.3 0.5 0.3 0.3 3")
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:end_rod ~ ~1.5 ~ 0.2 0.2 0.2 0.01 1")
                elif self.mode == "evil":
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:smoke ~ ~1 ~ 0.3 0.3 0.3 0.02 3")
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:flame ~ ~1.5 ~ 0.2 0.2 0.2 0.01 2")
                elif self.mode == "loki":
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:witch ~ ~1 ~ 0.3 0.4 0.3 0.01 2")
                    rcon_cmd(f"{pre}execute as {sel} at @s run particle minecraft:firework ~ ~2 ~ 0.2 0.1 0.2 0.05 1")
            except Exception:
                pass
            self._stop.wait(3)

    def _start_roam_loop(self):
        if self._roam_thread and self._roam_thread.is_alive():
            return
        self._roam_thread = threading.Thread(target=self._roam_loop, daemon=True)
        self._roam_thread.start()

    def _get_other_npcs(self):
        """Get list of other running NPCs (for inter-NPC interaction)."""
        others = []
        try:
            for nid, npc in _npcs.items():
                if npc is not self and npc.running:
                    others.append(npc)
        except Exception:
            pass
        return others

    def _npc_interact(self, other_npc):
        """Generate an interaction between this NPC and another NPC using AI."""
        import random

        # Guard: never interact with yourself
        if other_npc is self or other_npc.npc_id == self.npc_id:
            return

        # Walk toward the other NPC's position
        try:
            other_pos = other_npc._get_entity_pos()
            if other_pos:
                self._walk_to_player_pos(other_pos[0], other_pos[1], other_pos[2], other_npc.home_dimension)
        except Exception:
            pass

        # Build a prompt about the encounter
        my_name = self.npc_name
        their_name = other_npc.npc_name
        their_mode = other_npc.mode

        encounter_prompts = [
            f"You just encountered {their_name} (a {'benevolent god' if their_mode == 'good' else 'dark god' if their_mode == 'evil' else 'trickster god'}). "
            f"React to their presence! Taunt them, challenge them, ally with them, or fight them. Be dramatic.",
            f"{their_name} is right in front of you. They are a {their_mode} deity. "
            f"What do you say or do? You could attack their territory, mock them, propose an alliance, or show dominance.",
            f"You've wandered into {their_name}'s domain. Do you provoke them, show off your power, or try to outdo them?",
            f"{their_name} is nearby. As a god, you can't let that stand. Assert your dominance, challenge them, or scheme.",
        ]

        prompt = random.choice(encounter_prompts)
        raw = self._ask_mlx(f"__npc_{other_npc.npc_id}__", prompt)
        self._execute_actions(raw, default_player="@a")
        clean = self._strip_actions(raw)
        if clean and clean != "...":
            self._say("@a", clean)

        # Battle visual effects between the two gods
        try:
            my_pos = self._get_entity_pos()
            their_pos = other_npc._get_entity_pos()
            if my_pos and their_pos:
                mid_x = int((my_pos[0] + their_pos[0]) / 2)
                mid_y = int(max(my_pos[1], their_pos[1]) + 3)
                mid_z = int((my_pos[2] + their_pos[2]) / 2)
                pre = self._dim_prefix()
                # Epic clash particles
                rcon_cmd(f'{pre}execute positioned {mid_x} {mid_y} {mid_z} run particle minecraft:explosion ~ ~ ~ 2 2 2 0 5')
                rcon_cmd(f'{pre}execute positioned {mid_x} {mid_y} {mid_z} run particle minecraft:firework ~ ~ ~ 3 3 3 0.1 30')
                rcon_cmd(f'{pre}execute positioned {mid_x} {mid_y} {mid_z} run particle minecraft:end_rod ~ ~ ~ 2 2 2 0.05 20')
                # Lightning strikes between them
                rcon_cmd(f'{pre}summon minecraft:lightning_bolt {mid_x} {mid_y} {mid_z}')
                rcon_cmd(f'{pre}playsound minecraft:entity.wither.ambient master @a {mid_x} {mid_y} {mid_z} 1 0.5')
                # Announce the clash
                rcon_cmd(f'tellraw @a [{{"text":"[GOD CLASH] ","color":"red","bold":true}},'
                         f'{{"text":"{my_name}","color":"{"light_purple" if self.mode == "good" else "dark_red" if self.mode == "evil" else "gold"}"}},'
                         f'{{"text":" vs ","color":"white"}},'
                         f'{{"text":"{their_name}","color":"{"light_purple" if other_npc.mode == "good" else "dark_red" if other_npc.mode == "evil" else "gold"}"}},'
                         f'{{"text":"!","color":"red","bold":true}}]')
        except Exception:
            pass

        import time as _t
        _t.sleep(1.0)

        # The other NPC responds
        response_prompts = [
            f"{my_name} (a {'benevolent god' if self.mode == 'good' else 'dark god' if self.mode == 'evil' else 'trickster god'}) "
            f'just said to you: "{clean}". Respond! Defend yourself, counter-attack, mock them, or one-up them. '
            f"Use your god powers dramatically. You are in a BATTLE OF THE GODS.",
            f"{my_name} is challenging you! They said: \"{clean}\". "
            f"How do you respond? Use your god powers to show them who's boss. Be THEATRICAL and POWERFUL.",
            f"A divine battle has begun! {my_name} struck first with: \"{clean}\". "
            f"Retaliate with overwhelming force! Use terrain, weather, explosions, anything!",
        ]

        counter_prompt = random.choice(response_prompts)
        counter_raw = other_npc._ask_mlx(f"__npc_{self.npc_id}__", counter_prompt)
        other_npc._execute_actions(counter_raw, default_player="@a")
        counter_clean = other_npc._strip_actions(counter_raw)
        if counter_clean and counter_clean != "...":
            other_npc._say("@a", counter_clean)

        # More battle effects after the response
        try:
            if my_pos and their_pos:
                rcon_cmd(f'{pre}execute positioned {mid_x} {mid_y + 5} {mid_z} run particle minecraft:flash ~ ~ ~ 0 0 0 0 1')
                rcon_cmd(f'{pre}execute positioned {mid_x} {mid_y} {mid_z} run particle minecraft:witch ~ ~ ~ 4 4 4 0.02 40')
        except Exception:
            pass

        # Log the encounter in full
        ts = time.strftime("%H:%M:%S")
        for log in (self._chat_log, other_npc._chat_log):
            log.append({
                "type": "god_clash",
                "text": f"[GOD CLASH] {my_name} vs {their_name}",
                "speaker": my_name, "listener": their_name,
                "time": ts,
            })
            log.append({
                "type": "god_clash",
                "text": clean[:300],
                "speaker": my_name, "listener": their_name,
                "time": ts,
            })
            log.append({
                "type": "god_clash",
                "text": counter_clean[:300],
                "speaker": their_name, "listener": my_name,
                "time": ts,
            })

        # Both gods update their soul — opinions forged in battle
        self._update_god_opinion(their_name, f"We clashed. I said: \"{clean[:60]}\" — Rival.")
        other_npc._update_god_opinion(my_name, f"We clashed. Said: \"{clean[:60]}\" — Adversary.")

    def _walk_to_player_pos(self, tx, ty, tz, tdim=None):
        """Walk entity toward specific coordinates."""
        tdim = tdim or self.home_dimension
        cur = self._get_entity_pos()
        sx = cur[0] if cur else self.home_x
        sy = cur[1] if cur else self._entity_y
        sz = cur[2] if cur else self.home_z

        if self.home_dimension != tdim:
            self._despawn_entity()
            self.home_dimension = tdim

        stop_x = tx + (3 if tx > sx else -3)
        stop_z = tz + (3 if tz > sz else -3)
        stop_y = ty

        dist = ((stop_x - sx)**2 + (stop_z - sz)**2) ** 0.5
        steps = max(4, min(15, int(dist / 1.5)))
        for i in range(1, steps + 1):
            if self._stop.is_set():
                return
            t = i / steps
            self._tp_entity(sx + (stop_x - sx) * t, sy + (stop_y - sy) * t, sz + (stop_z - sz) * t, tdim)
            import time as _t
            _t.sleep(0.15)

        self.home_x = stop_x
        self.home_z = stop_z
        self._entity_y = stop_y

    def _roam_loop(self):
        """Autonomous roaming: wander, find players, interact with other NPCs, do mode-appropriate actions."""
        import random
        tag = self._entity_tag
        while not self._stop.is_set() and self.running:
            self._stop.wait(self.roam_interval)
            if self._stop.is_set() or not self.running or not self.auto_roam:
                continue
            try:
                pre = self._dim_prefix()
                plist = rcon_cmd("list")
                players = []
                if plist and ":" in plist:
                    names = plist.split(":")[1].strip()
                    if names:
                        players = [n.strip() for n in names.split(",") if n.strip()]

                mode_info = self.MODES.get(self.mode, self.MODES["good"])
                other_npcs = self._get_other_npcs()
                has_players = len(players) > 0

                # ── Always: finish any active god conversation first ──
                continued_convo = False
                if self.auto_god_chat and other_npcs:
                    # Check group conversations first (this god is a participant)
                    for ckey, convo in list(NpcChat._active_convos.items()):
                        if self.npc_id in ckey and convo.get("group") and convo["turns"] < convo.get("max_turns", 8):
                            group_gods = [_npcs[gid] for gid in ckey if gid in _npcs and _npcs[gid].running]
                            if len(group_gods) >= 3:
                                self._group_god_chat(group_gods)
                                continued_convo = True
                                break
                    # Then check pair conversations
                    if not continued_convo:
                        for other in other_npcs:
                            convo_key = frozenset({self.npc_id, other.npc_id})
                            convo = NpcChat._active_convos.get(convo_key)
                            if convo and convo["turns"] < convo.get("max_turns", 6):
                                self._god_to_god_chat(other)
                                continued_convo = True
                                break
                    if continued_convo:
                        if random.random() < 0.05:
                            self._wander_nearby()
                        continue

                # ══════════════════════════════════════════════════════
                # NO PLAYERS ONLINE — heavy building focus, chat between builds
                # ══════════════════════════════════════════════════════
                if not has_players:
                    roll = random.random()

                    if roll < 0.35 and self.auto_build and other_npcs:
                        # 35% — collab build with another god (primary activity)
                        other = random.choice(other_npcs)
                        self._collab_build(other)
                        continue

                    elif roll < 0.55 and self.auto_build:
                        # 20% — solo build
                        self._ai_solo_build()
                        continue

                    elif roll < 0.85 and self.auto_god_chat and other_npcs:
                        # 30% — start a new god conversation (group if 3+ available, else pair)
                        all_running = [n for n in _npcs.values() if n.running and n is not self]
                        if len(all_running) >= 2 and random.random() < 0.45:
                            self._group_god_chat(all_running)
                        else:
                            other = random.choice(other_npcs)
                            self._god_to_god_chat(other)
                        continue

                    elif roll < 0.92:
                        # 7% — reflect and journal
                        self._reflect()
                        continue

                    elif roll < 0.97 and self.auto_clash and other_npcs:
                        # 5% — clash if enabled
                        other = random.choice(other_npcs)
                        self._npc_interact(other)
                        continue

                    else:
                        # 3-8% — wander
                        self._wander_nearby()
                        continue

                # ══════════════════════════════════════════════════════
                # PLAYERS ONLINE — mix player focus with god activities
                # ══════════════════════════════════════════════════════
                roll = random.random()

                # 15% — collab build (still building while players watch)
                if roll < 0.15 and self.auto_build and other_npcs:
                    other = random.choice(other_npcs)
                    self._collab_build(other)
                    continue

                # 10% — solo build
                elif roll < 0.25 and self.auto_build:
                    self._ai_solo_build()
                    continue

                # 15% — god chat (group if 3+ available, else pair)
                elif roll < 0.40 and self.auto_god_chat and other_npcs:
                    all_running = [n for n in _npcs.values() if n.running and n is not self]
                    if len(all_running) >= 2 and random.random() < 0.40:
                        self._group_god_chat(all_running)
                    else:
                        other = random.choice(other_npcs)
                        self._god_to_god_chat(other)
                    continue

                # 5% — reflect
                elif roll < 0.45:
                    self._reflect()
                    continue

                # 5% — clash if enabled
                elif roll < 0.50 and self.auto_clash and other_npcs:
                    other = random.choice(other_npcs)
                    self._npc_interact(other)
                    continue

                # 50% — focus on players (roam actions, walk to them, interact)
                target = random.choice(players)

                # LOKI AUTONOMOUS MODE: Loki uses its own AI brain
                if self.mode == "loki" and mode_info.get("autonomous"):
                    self._loki_autonomous_action(target, players)
                    continue

                # Movement: 50% wander nearby, 40% walk to player, 10% stay put
                move_roll = random.random()
                if move_roll < 0.50:
                    self._wander_nearby()
                elif move_roll < 0.90:
                    self._walk_to_player(target)

                action = random.choice(mode_info["roam_actions"])
                msg = random.choice(mode_info["roam_msgs"])
                rcon_cmd(action)
                self._say("@a", msg)

                self._chat_log.append({
                    "type": "system",
                    "text": f"[ROAM] {msg}",
                    "time": time.strftime("%H:%M:%S"),
                })
            except Exception as e:
                self._chat_log.append({
                    "type": "error",
                    "text": f"Roam error: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    def _loki_autonomous_action(self, target, all_players):
        """Loki mode: Loki thinks for itself using the LLM, decides what to do."""
        import random
        mode_info = self.MODES["loki"]

        # Decide behavior: 40% AI-driven, 35% canned+wander, 10% fly-away, 15% lurk/wander
        roll = random.random()

        if roll < 0.40:
            # AI-driven autonomous action — rich contextual prompts
            # 70% walk to player, 30% wander nearby first
            if random.random() < 0.70:
                self._walk_to_player(target)
            else:
                self._wander_nearby()
            loki_prompts = [
                "You just walked up to {player}. They don't know you're here yet. What devious thing do you do?",
                "{player} is minding their own business. Time to cause chaos. What's your move, Trickster?",
                "You see {player} standing there. Trick them, surprise them, or mess with them creatively.",
                "{player} looks way too comfortable. Fix that. Be creative and theatrical about it.",
                "You're bored. {player} is your new toy. Do something unexpected and dramatic.",
                "It's too peaceful. {player} needs a 'gift' from the Trickster God. What do you give them?",
                "{player} just looked at you. They seem nervous. Make them MORE nervous... or surprise them with kindness (this time).",
                "Pick a fight with {player}. Not a real one — a FUNNY one. Taunt them and do something absurd.",
            ]
            prompt = random.choice(loki_prompts).format(player=target)
            if len(all_players) > 1:
                other = random.choice([p for p in all_players if p != target] or [target])
                prompt += f" ({other} is also nearby — you could drag them into this too.)"
            # Add awareness of other NPCs
            other_npcs = self._get_other_npcs()
            if other_npcs:
                npc_names = ", ".join(f"{n.npc_name} ({n.mode})" for n in other_npcs)
                prompt += f" Other gods present: {npc_names}. You could interact with them too!"

            raw_response = self._ask_mlx("__loki_auto__", prompt)
            self._execute_actions(raw_response, default_player=target)
            clean = self._strip_actions(raw_response)
            if clean and clean != "...":
                self._say("@a", clean)

            self._chat_log.append({
                "type": "system",
                "text": f"[LOKI AUTO] Target: {target} — {clean}",
                "time": time.strftime("%H:%M:%S"),
            })

        elif roll < 0.75:
            # Canned roam action — mostly wander, sometimes walk to player
            if random.random() < 0.60:
                self._wander_nearby()
            else:
                self._walk_to_player(target)
            action = random.choice(mode_info["roam_actions"])
            msg = random.choice(mode_info["roam_msgs"])
            rcon_cmd(action)
            self._say("@a", msg)
            self._chat_log.append({
                "type": "system",
                "text": f"[ROAM] {msg}",
                "time": time.strftime("%H:%M:%S"),
            })

        elif roll < 0.80:
            # BUILD A PRANK STRUCTURE near a player
            self._wander_nearby()
            self._ai_solo_build()

        elif roll < 0.88:
            # FLY AWAY — dramatic exit (rare)
            self._walk_to_player(target)
            taunt = random.choice([
                "You bore me, mortals! I'm off!",
                "Catch me if you can! Ahahahaha!",
                "The Trickster grows restless... FAREWELL!",
                "I have better things to do. Like NOTHING!",
                "Too easy. I need a challenge. BYE!",
                "The wind calls me away... just kidding, I just don't like you.",
                "Loki vanishes in a puff of chaos!",
            ])
            self._say("@a", taunt)
            self._execute_actions("[FLY]", default_player=target)
            self._chat_log.append({
                "type": "system",
                "text": f"[LOKI FLY] {taunt}",
                "time": time.strftime("%H:%M:%S"),
            })

        else:
            # Wander and lurk menacingly
            self._wander_nearby()
            lurk = random.choice([
                "The Trickster watches from the shadows...",
                "Oracle stares at you silently. Unsettling.",
                "You feel eyes on the back of your neck...",
                "Oracle wanders past, humming a strange tune...",
                "The Trickster paces back and forth, plotting...",
                "Oracle drifts by silently. What is it thinking?",
                "The air shimmers as Oracle strolls past...",
            ])
            self._say("@a", lurk)
            self._chat_log.append({
                "type": "system",
                "text": f"[LOKI WANDER] {lurk}",
                "time": time.strftime("%H:%M:%S"),
            })

    def _despawn_entity(self):
        for dim in ["overworld", "the_nether", "the_end"]:
            pre = self._dim_prefix(dim)
            rcon_cmd(f"{pre}kill @e[tag={self._entity_tag}]")

    def _tp_entity(self, x, y, z, dim=None):
        dim = dim or self.home_dimension
        pre = self._dim_prefix(dim)
        ix, iy, iz = int(x), int(y), int(z)
        result = rcon_cmd(f"{pre}tp @e[tag={self._entity_tag}] {ix} {iy} {iz}")
        if result and ("no entity" in result.lower() or "0 entities" in result.lower()):
            self.home_x, self.home_z, self._entity_y = x, z, y
            self.home_dimension = dim
            self._spawn_entity()
        else:
            self._entity_y = y

    def _get_entity_pos(self):
        """Return current (x, y, z) of the entity, or None."""
        try:
            pre = self._dim_prefix()
            resp = rcon_cmd(f"{pre}data get entity @e[tag={self._entity_tag},limit=1] Pos")
            m = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', resp or "")
            if m:
                return float(m.group(1)), float(m.group(2)), float(m.group(3))
        except Exception:
            pass
        return None

    def _walk_to_player(self, player_name):
        """Smoothly walk entity toward a player with many small steps."""
        try:
            pos_resp = rcon_cmd(f"data get entity {player_name} Pos")
            dim_resp = rcon_cmd(f"data get entity {player_name} Dimension")
            pos_match = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', pos_resp or "")
            dim_match = re.search(r'"minecraft:(\w+)"', dim_resp or "")
            if not pos_match:
                return
            px = float(pos_match.group(1))
            py = float(pos_match.group(2))
            pz = float(pos_match.group(3))
            pdim = dim_match.group(1) if dim_match else "overworld"

            cur = self._get_entity_pos()
            sx = cur[0] if cur else self.home_x
            sy = cur[1] if cur else self._entity_y
            sz = cur[2] if cur else self.home_z
            sdim = self.home_dimension

            if sdim != pdim:
                self._despawn_entity()
                self.home_dimension = pdim

            # Stop 3 blocks away from the player
            stop_x = px + (3 if px > sx else -3)
            stop_z = pz + (3 if pz > sz else -3)
            stop_y = py

            # Calculate distance
            dist = ((stop_x - sx)**2 + (stop_z - sz)**2) ** 0.5

            # More steps for longer distances — looks like walking
            # ~1.5 blocks per step, min 6 steps, max 20
            steps = max(6, min(20, int(dist / 1.5)))
            step_delay = 0.15  # Fast small hops look like walking

            for i in range(1, steps + 1):
                if self._stop.is_set():
                    return
                t = i / steps
                cx = sx + (stop_x - sx) * t
                cy = sy + (stop_y - sy) * t
                cz = sz + (stop_z - sz) * t
                self._tp_entity(cx, cy, cz, pdim)
                time.sleep(step_delay)

            self.home_x = stop_x
            self.home_z = stop_z
            self._entity_y = stop_y
            self.home_dimension = pdim
        except Exception as e:
            self._chat_log.append({
                "type": "error",
                "text": f"Walk failed: {e}",
                "time": time.strftime("%H:%M:%S"),
            })

    def _wander_nearby(self):
        """Wander a short distance from current position — casual strolling."""
        import random
        cur = self._get_entity_pos()
        if not cur:
            return
        ox, oy, oz = cur
        # Small random offset (5-15 blocks)
        dx = random.randint(-15, 15)
        dz = random.randint(-15, 15)
        nx, nz = ox + dx, oz + dz

        dist = (dx**2 + dz**2) ** 0.5
        steps = max(4, min(12, int(dist / 1.5)))
        for i in range(1, steps + 1):
            if self._stop.is_set():
                return
            t = i / steps
            cx = ox + dx * t
            cz = oz + dz * t
            self._tp_entity(cx, oy, cz)
            time.sleep(0.15)

        self.home_x = nx
        self.home_z = nz

    # ------------------------------------------------------------------ #
    # STRUCTURE BUILDING SYSTEM                                            #
    # ------------------------------------------------------------------ #

    # Each structure is a list of RCON commands with {x},{y},{z} placeholders
    # for the origin. Structures are built block-by-block with delays for drama.

    _GOOD_STRUCTURES = [
        {
            "name": "Golden Shrine",
            "announce": "The Oracle begins to construct a shrine of light...",
            "subterranean": False,
            "commands": [
                # Platform
                "fill {x}-3 {y} {z}-3 {x}+3 {y} {z}+3 minecraft:gold_block",
                "fill {x}-2 {y} {z}-2 {x}+2 {y} {z}+2 minecraft:glowstone",
                # Pillars
                "fill {x}-3 {y}+1 {z}-3 {x}-3 {y}+5 {z}-3 minecraft:quartz_pillar",
                "fill {x}+3 {y}+1 {z}-3 {x}+3 {y}+5 {z}-3 minecraft:quartz_pillar",
                "fill {x}-3 {y}+1 {z}+3 {x}-3 {y}+5 {z}+3 minecraft:quartz_pillar",
                "fill {x}+3 {y}+1 {z}+3 {x}+3 {y}+5 {z}+3 minecraft:quartz_pillar",
                # Roof beams
                "fill {x}-3 {y}+6 {z}-3 {x}+3 {y}+6 {z}-3 minecraft:smooth_quartz_slab",
                "fill {x}-3 {y}+6 {z}+3 {x}+3 {y}+6 {z}+3 minecraft:smooth_quartz_slab",
                "fill {x}-3 {y}+6 {z}-3 {x}-3 {y}+6 {z}+3 minecraft:smooth_quartz_slab",
                "fill {x}+3 {y}+6 {z}-3 {x}+3 {y}+6 {z}+3 minecraft:smooth_quartz_slab",
                # Peaked roof
                "fill {x}-2 {y}+7 {z}-2 {x}+2 {y}+7 {z}+2 minecraft:gold_block",
                "fill {x}-1 {y}+8 {z}-1 {x}+1 {y}+8 {z}+1 minecraft:glowstone",
                "setblock {x} {y}+9 {z} minecraft:beacon",
                # Lanterns
                "setblock {x}-2 {y}+4 {z}-2 minecraft:lantern",
                "setblock {x}+2 {y}+4 {z}-2 minecraft:lantern",
                "setblock {x}-2 {y}+4 {z}+2 minecraft:lantern",
                "setblock {x}+2 {y}+4 {z}+2 minecraft:lantern",
            ],
        },
        {
            "name": "Crystal Garden",
            "announce": "The Oracle weaves a garden of crystal and bloom...",
            "subterranean": False,
            "commands": [
                "fill {x}-5 {y}-1 {z}-5 {x}+5 {y}-1 {z}+5 minecraft:moss_block",
                "fill {x}-4 {y} {z}-4 {x}+4 {y} {z}+4 minecraft:air",
                "setblock {x}-3 {y} {z}-2 minecraft:amethyst_cluster[facing=up]",
                "setblock {x}+2 {y} {z}-3 minecraft:amethyst_cluster[facing=up]",
                "setblock {x}-1 {y} {z}+3 minecraft:amethyst_cluster[facing=up]",
                "setblock {x}+3 {y} {z}+1 minecraft:amethyst_cluster[facing=up]",
                "fill {x}-1 {y} {z}-1 {x}+1 {y}+2 {z}+1 minecraft:amethyst_block",
                "setblock {x} {y}+3 {z} minecraft:amethyst_cluster[facing=up]",
                "setblock {x}-4 {y} {z} minecraft:flowering_azalea",
                "setblock {x}+4 {y} {z} minecraft:flowering_azalea",
                "setblock {x} {y} {z}-4 minecraft:azalea",
                "setblock {x} {y} {z}+4 minecraft:flowering_azalea",
                "setblock {x}-2 {y} {z}-4 minecraft:cornflower",
                "setblock {x}+3 {y} {z}+3 minecraft:allium",
                "setblock {x}-3 {y} {z}+2 minecraft:lily_of_the_valley",
                "fill {x}-2 {y}-1 {z} {x}+2 {y}-1 {z} minecraft:water",
                "setblock {x} {y}-1 {z}-2 minecraft:sea_lantern",
                "setblock {x} {y}-1 {z}+2 minecraft:sea_lantern",
            ],
        },
        {
            "name": "Enchanted Underground Vault",
            "announce": "The Oracle senses something hidden below... and begins to dig.",
            "subterranean": True,
            "commands": [
                # Dig chamber
                "fill {x}-4 {y}-8 {z}-4 {x}+4 {y}-2 {z}+4 minecraft:air",
                # Walls
                "fill {x}-5 {y}-9 {z}-5 {x}+5 {y}-1 {z}-5 minecraft:deepslate_bricks",
                "fill {x}-5 {y}-9 {z}+5 {x}+5 {y}-1 {z}+5 minecraft:deepslate_bricks",
                "fill {x}-5 {y}-9 {z}-5 {x}-5 {y}-1 {z}+5 minecraft:deepslate_bricks",
                "fill {x}+5 {y}-9 {z}-5 {x}+5 {y}-1 {z}+5 minecraft:deepslate_bricks",
                # Floor
                "fill {x}-4 {y}-9 {z}-4 {x}+4 {y}-9 {z}+4 minecraft:gilded_blackstone",
                # Ceiling
                "fill {x}-4 {y}-1 {z}-4 {x}+4 {y}-1 {z}+4 minecraft:deepslate_tiles",
                # Inner air
                "fill {x}-4 {y}-8 {z}-4 {x}+4 {y}-2 {z}+4 minecraft:air",
                # Staircase entrance
                "fill {x} {y}-1 {z}-5 {x} {y} {z}-5 minecraft:air",
                "setblock {x} {y}-1 {z}-4 minecraft:deepslate_brick_stairs[facing=south]",
                "setblock {x} {y}-2 {z}-3 minecraft:deepslate_brick_stairs[facing=south]",
                "setblock {x} {y}-3 {z}-2 minecraft:deepslate_brick_stairs[facing=south]",
                # Treasure
                "setblock {x} {y}-8 {z} minecraft:chest",
                "setblock {x}-3 {y}-8 {z}-3 minecraft:enchanting_table",
                # Lighting
                "setblock {x}-3 {y}-3 {z}-3 minecraft:soul_lantern",
                "setblock {x}+3 {y}-3 {z}-3 minecraft:soul_lantern",
                "setblock {x}-3 {y}-3 {z}+3 minecraft:soul_lantern",
                "setblock {x}+3 {y}-3 {z}+3 minecraft:soul_lantern",
                # Center pillar
                "fill {x} {y}-8 {z} {x} {y}-3 {z} minecraft:amethyst_block",
                "setblock {x} {y}-2 {z} minecraft:beacon",
            ],
        },
        {
            "name": "Celestial Watchtower",
            "announce": "The Oracle raises a watchtower to survey the realm...",
            "subterranean": False,
            "commands": [
                # Foundation
                "fill {x}-2 {y} {z}-2 {x}+2 {y} {z}+2 minecraft:smooth_stone",
                # Tower shaft
                "fill {x}-2 {y}+1 {z}-2 {x}+2 {y}+15 {z}-2 minecraft:white_concrete",
                "fill {x}-2 {y}+1 {z}+2 {x}+2 {y}+15 {z}+2 minecraft:white_concrete",
                "fill {x}-2 {y}+1 {z}-2 {x}-2 {y}+15 {z}+2 minecraft:white_concrete",
                "fill {x}+2 {y}+1 {z}-2 {x}+2 {y}+15 {z}+2 minecraft:white_concrete",
                # Hollow inside
                "fill {x}-1 {y}+1 {z}-1 {x}+1 {y}+14 {z}+1 minecraft:air",
                # Spiral stairs (simplified)
                "setblock {x}-1 {y}+1 {z} minecraft:oak_stairs[facing=east]",
                "setblock {x} {y}+2 {z}-1 minecraft:oak_stairs[facing=south]",
                "setblock {x}+1 {y}+3 {z} minecraft:oak_stairs[facing=west]",
                "setblock {x} {y}+4 {z}+1 minecraft:oak_stairs[facing=north]",
                "setblock {x}-1 {y}+5 {z} minecraft:oak_stairs[facing=east]",
                "setblock {x} {y}+6 {z}-1 minecraft:oak_stairs[facing=south]",
                "setblock {x}+1 {y}+7 {z} minecraft:oak_stairs[facing=west]",
                "setblock {x} {y}+8 {z}+1 minecraft:oak_stairs[facing=north]",
                "setblock {x}-1 {y}+9 {z} minecraft:oak_stairs[facing=east]",
                "setblock {x} {y}+10 {z}-1 minecraft:oak_stairs[facing=south]",
                "setblock {x}+1 {y}+11 {z} minecraft:oak_stairs[facing=west]",
                "setblock {x} {y}+12 {z}+1 minecraft:oak_stairs[facing=north]",
                # Observation deck
                "fill {x}-3 {y}+15 {z}-3 {x}+3 {y}+15 {z}+3 minecraft:smooth_quartz",
                "fill {x}-3 {y}+16 {z}-3 {x}+3 {y}+16 {z}-3 minecraft:glass",
                "fill {x}-3 {y}+16 {z}+3 {x}+3 {y}+16 {z}+3 minecraft:glass",
                "fill {x}-3 {y}+16 {z}-3 {x}-3 {y}+16 {z}+3 minecraft:glass",
                "fill {x}+3 {y}+16 {z}-3 {x}+3 {y}+16 {z}+3 minecraft:glass",
                "fill {x}-3 {y}+17 {z}-3 {x}+3 {y}+17 {z}+3 minecraft:white_concrete",
                # Beacon top
                "setblock {x} {y}+18 {z} minecraft:sea_lantern",
                "setblock {x} {y}+19 {z} minecraft:end_rod[facing=up]",
            ],
        },
        {
            "name": "Healing Springs Sanctum",
            "announce": "The Oracle hollows out a sacred spring deep below...",
            "subterranean": True,
            "commands": [
                # Dig cavern
                "fill {x}-6 {y}-12 {z}-6 {x}+6 {y}-3 {z}+6 minecraft:air",
                # Shell
                "fill {x}-7 {y}-13 {z}-7 {x}+7 {y}-2 {z}+7 minecraft:calcite hollow",
                # Inner air
                "fill {x}-6 {y}-12 {z}-6 {x}+6 {y}-3 {z}+6 minecraft:air",
                # Pool
                "fill {x}-3 {y}-12 {z}-3 {x}+3 {y}-12 {z}+3 minecraft:prismarine",
                "fill {x}-2 {y}-11 {z}-2 {x}+2 {y}-11 {z}+2 minecraft:water",
                "fill {x}-3 {y}-12 {z}-3 {x}-3 {y}-12 {z}+3 minecraft:sea_lantern",
                "fill {x}+3 {y}-12 {z}-3 {x}+3 {y}-12 {z}+3 minecraft:sea_lantern",
                # Columns
                "fill {x}-5 {y}-12 {z}-5 {x}-5 {y}-4 {z}-5 minecraft:quartz_pillar",
                "fill {x}+5 {y}-12 {z}-5 {x}+5 {y}-4 {z}-5 minecraft:quartz_pillar",
                "fill {x}-5 {y}-12 {z}+5 {x}-5 {y}-4 {z}+5 minecraft:quartz_pillar",
                "fill {x}+5 {y}-12 {z}+5 {x}+5 {y}-4 {z}+5 minecraft:quartz_pillar",
                # Glowing flora
                "setblock {x}-4 {y}-11 {z} minecraft:glow_lichen[up=true]",
                "setblock {x}+4 {y}-11 {z} minecraft:glow_lichen[up=true]",
                "setblock {x} {y}-11 {z}-4 minecraft:glow_lichen[up=true]",
                "setblock {x} {y}-11 {z}+4 minecraft:glow_lichen[up=true]",
                # Entrance shaft
                "fill {x} {y}-2 {z}-6 {x} {y} {z}-7 minecraft:air",
                "fill {x} {y}-12 {z}-6 {x} {y}-3 {z}-6 minecraft:ladder[facing=south]",
            ],
        },
    ]

    _EVIL_STRUCTURES = [
        {
            "name": "Obsidian Fortress",
            "announce": "The Mad God raises a fortress of darkness...",
            "subterranean": False,
            "commands": [
                "fill {x}-5 {y} {z}-5 {x}+5 {y} {z}+5 minecraft:blackstone",
                "fill {x}-5 {y}+1 {z}-5 {x}+5 {y}+8 {z}-5 minecraft:obsidian",
                "fill {x}-5 {y}+1 {z}+5 {x}+5 {y}+8 {z}+5 minecraft:obsidian",
                "fill {x}-5 {y}+1 {z}-5 {x}-5 {y}+8 {z}+5 minecraft:obsidian",
                "fill {x}+5 {y}+1 {z}-5 {x}+5 {y}+8 {z}+5 minecraft:obsidian",
                "fill {x}-4 {y}+1 {z}-4 {x}+4 {y}+7 {z}+4 minecraft:air",
                # Towers at corners
                "fill {x}-5 {y}+8 {z}-5 {x}-4 {y}+12 {z}-4 minecraft:crying_obsidian",
                "fill {x}+4 {y}+8 {z}-5 {x}+5 {y}+12 {z}-4 minecraft:crying_obsidian",
                "fill {x}-5 {y}+8 {z}+4 {x}-4 {y}+12 {z}+5 minecraft:crying_obsidian",
                "fill {x}+4 {y}+8 {z}+4 {x}+5 {y}+12 {z}+5 minecraft:crying_obsidian",
                # Lava moat
                "fill {x}-7 {y}-1 {z}-7 {x}+7 {y}-1 {z}-7 minecraft:lava",
                "fill {x}-7 {y}-1 {z}+7 {x}+7 {y}-1 {z}+7 minecraft:lava",
                "fill {x}-7 {y}-1 {z}-7 {x}-7 {y}-1 {z}+7 minecraft:lava",
                "fill {x}+7 {y}-1 {z}-7 {x}+7 {y}-1 {z}+7 minecraft:lava",
                # Gate
                "fill {x}-1 {y}+1 {z}-5 {x}+1 {y}+3 {z}-5 minecraft:air",
                # Nether flames
                "setblock {x}-4 {y}+1 {z}-4 minecraft:soul_fire",
                "setblock {x}+4 {y}+1 {z}-4 minecraft:soul_fire",
                "setblock {x}-4 {y}+1 {z}+4 minecraft:soul_fire",
                "setblock {x}+4 {y}+1 {z}+4 minecraft:soul_fire",
                # Skulls
                "setblock {x}-1 {y}+4 {z}-5 minecraft:skeleton_skull",
                "setblock {x}+1 {y}+4 {z}-5 minecraft:skeleton_skull",
                # Throne
                "setblock {x} {y}+1 {z}+3 minecraft:blackstone_stairs[facing=south]",
                "setblock {x} {y}+2 {z}+4 minecraft:polished_blackstone_wall",
            ],
        },
        {
            "name": "Nether Crypt",
            "announce": "The Mad God carves a crypt into the earth's bowels...",
            "subterranean": True,
            "commands": [
                # Excavate
                "fill {x}-5 {y}-10 {z}-5 {x}+5 {y}-2 {z}+5 minecraft:air",
                # Walls
                "fill {x}-6 {y}-11 {z}-6 {x}+6 {y}-1 {z}+6 minecraft:nether_bricks hollow",
                "fill {x}-5 {y}-10 {z}-5 {x}+5 {y}-2 {z}+5 minecraft:air",
                # Floor
                "fill {x}-5 {y}-11 {z}-5 {x}+5 {y}-11 {z}+5 minecraft:magma_block",
                # Coffins (stone slab bases)
                "fill {x}-3 {y}-10 {z}-1 {x}-2 {y}-10 {z}+1 minecraft:polished_deepslate",
                "fill {x}+2 {y}-10 {z}-1 {x}+3 {y}-10 {z}+1 minecraft:polished_deepslate",
                # Lava channels
                "setblock {x}-4 {y}-10 {z}-4 minecraft:lava",
                "setblock {x}+4 {y}-10 {z}-4 minecraft:lava",
                "setblock {x}-4 {y}-10 {z}+4 minecraft:lava",
                "setblock {x}+4 {y}-10 {z}+4 minecraft:lava",
                # Soul fire braziers
                "setblock {x}-4 {y}-10 {z} minecraft:soul_campfire",
                "setblock {x}+4 {y}-10 {z} minecraft:soul_campfire",
                # Altar
                "fill {x}-1 {y}-10 {z}+3 {x}+1 {y}-10 {z}+4 minecraft:polished_blackstone",
                "setblock {x} {y}-9 {z}+4 minecraft:respawn_anchor",
                # Entrance
                "fill {x} {y}-2 {z}-6 {x} {y} {z}-6 minecraft:air",
                "fill {x} {y}-10 {z}-6 {x} {y}-3 {z}-6 minecraft:nether_brick_stairs[facing=south]",
                # Skulls
                "setblock {x}-1 {y}-5 {z}-5 minecraft:wither_skeleton_skull",
                "setblock {x}+1 {y}-5 {z}-5 minecraft:wither_skeleton_skull",
            ],
        },
        {
            "name": "Corrupted Spire",
            "announce": "The earth screams as the Mad God raises a spire of corruption...",
            "subterranean": False,
            "commands": [
                "fill {x}-3 {y} {z}-3 {x}+3 {y} {z}+3 minecraft:netherrack",
                "fill {x}-2 {y}+1 {z}-2 {x}+2 {y}+6 {z}+2 minecraft:blackstone",
                "fill {x}-1 {y}+1 {z}-1 {x}+1 {y}+5 {z}+1 minecraft:air",
                "fill {x}-1 {y}+7 {z}-1 {x}+1 {y}+14 {z}+1 minecraft:crying_obsidian",
                "fill {x} {y}+7 {z} {x} {y}+13 {z} minecraft:air",
                "setblock {x} {y}+15 {z} minecraft:respawn_anchor",
                "setblock {x} {y}+16 {z} minecraft:soul_fire",
                # Surrounding corruption
                "fill {x}-6 {y}-1 {z}-6 {x}+6 {y}-1 {z}+6 minecraft:netherrack",
                "setblock {x}-4 {y} {z}-4 minecraft:fire",
                "setblock {x}+4 {y} {z}+4 minecraft:fire",
                "setblock {x}-4 {y} {z}+4 minecraft:fire",
                "setblock {x}+4 {y} {z}-4 minecraft:fire",
                "setblock {x}-3 {y} {z} minecraft:soul_fire",
                "setblock {x}+3 {y} {z} minecraft:soul_fire",
                # Webs
                "setblock {x}-2 {y}+3 {z} minecraft:cobweb",
                "setblock {x}+2 {y}+3 {z} minecraft:cobweb",
            ],
        },
        {
            "name": "Wither Dungeon",
            "announce": "The Mad God hollows out a dungeon of despair below...",
            "subterranean": True,
            "commands": [
                "fill {x}-7 {y}-15 {z}-7 {x}+7 {y}-3 {z}+7 minecraft:air",
                "fill {x}-8 {y}-16 {z}-8 {x}+8 {y}-2 {z}+8 minecraft:deepslate hollow",
                "fill {x}-7 {y}-15 {z}-7 {x}+7 {y}-3 {z}+7 minecraft:air",
                # Floor
                "fill {x}-7 {y}-16 {z}-7 {x}+7 {y}-16 {z}+7 minecraft:sculk",
                # Cell cages
                "fill {x}-6 {y}-15 {z}-6 {x}-4 {y}-12 {z}-4 minecraft:iron_bars",
                "fill {x}-5 {y}-15 {z}-5 {x}-5 {y}-13 {z}-5 minecraft:air",
                "fill {x}+4 {y}-15 {z}-6 {x}+6 {y}-12 {z}-4 minecraft:iron_bars",
                "fill {x}+5 {y}-15 {z}-5 {x}+5 {y}-13 {z}-5 minecraft:air",
                # Chains
                "fill {x} {y}-3 {z} {x} {y}-8 {z} minecraft:chain",
                # Sculk sensors
                "setblock {x}-3 {y}-15 {z} minecraft:sculk_sensor",
                "setblock {x}+3 {y}-15 {z} minecraft:sculk_sensor",
                # Mob spawner in center
                "setblock {x} {y}-15 {z} minecraft:spawner",
                # Entrance
                "fill {x} {y}-2 {z}-8 {x} {y} {z}-8 minecraft:air",
                "fill {x} {y}-15 {z}-8 {x} {y}-3 {z}-8 minecraft:ladder[facing=south]",
            ],
        },
    ]

    _LOKI_STRUCTURES = [
        {
            "name": "Upside-Down House",
            "announce": "Loki giggles and begins building... something... wrong...",
            "subterranean": False,
            "commands": [
                # Roof on the ground
                "fill {x}-3 {y} {z}-3 {x}+3 {y} {z}+3 minecraft:spruce_stairs[facing=south]",
                # Walls going up but inverted materials
                "fill {x}-3 {y}+1 {z}-3 {x}+3 {y}+4 {z}-3 minecraft:glass",
                "fill {x}-3 {y}+1 {z}+3 {x}+3 {y}+4 {z}+3 minecraft:glass",
                "fill {x}-3 {y}+1 {z}-3 {x}-3 {y}+4 {z}+3 minecraft:glass",
                "fill {x}+3 {y}+1 {z}-3 {x}+3 {y}+4 {z}+3 minecraft:glass",
                "fill {x}-2 {y}+1 {z}-2 {x}+2 {y}+3 {z}+2 minecraft:air",
                # Floor on top
                "fill {x}-3 {y}+5 {z}-3 {x}+3 {y}+5 {z}+3 minecraft:grass_block",
                # Furniture on ceiling
                "setblock {x} {y}+4 {z} minecraft:crafting_table",
                "setblock {x}+1 {y}+4 {z} minecraft:furnace",
                "setblock {x}-1 {y}+4 {z} minecraft:chest",
                # Trees growing down from "floor"
                "fill {x}+2 {y}+1 {z}+2 {x}+2 {y}+4 {z}+2 minecraft:oak_log",
                "fill {x}+1 {y}+1 {z}+1 {x}+3 {y}+2 {z}+3 minecraft:oak_leaves",
                # Door in the wrong place
                "setblock {x} {y}+5 {z}-3 minecraft:oak_door[half=lower]",
                "setblock {x} {y}+6 {z}-3 minecraft:oak_door[half=upper]",
                # Chickens inside
                "execute positioned {x} {y}+2 {z} run summon minecraft:chicken",
                "execute positioned {x}+1 {y}+2 {z}-1 run summon minecraft:chicken",
            ],
        },
        {
            "name": "The Prank Pit",
            "announce": "Loki whistles innocently while digging a 'totally safe' hole...",
            "subterranean": True,
            "commands": [
                # Nice looking surface
                "fill {x}-3 {y} {z}-3 {x}+3 {y} {z}+3 minecraft:grass_block",
                "setblock {x} {y}+1 {z} minecraft:dandelion",
                "setblock {x}+1 {y}+1 {z}+1 minecraft:poppy",
                # Looks like a flower garden BUT...
                # Thin floor
                "fill {x}-2 {y}-1 {z}-2 {x}+2 {y}-1 {z}+2 minecraft:sand",
                # Huge pit underneath
                "fill {x}-2 {y}-20 {z}-2 {x}+2 {y}-2 {z}+2 minecraft:air",
                # Water at the very bottom (not lethal... barely)
                "fill {x}-2 {y}-20 {z}-2 {x}+2 {y}-20 {z}+2 minecraft:water",
                # Signs that taunt
                "setblock {x}-2 {y}-10 {z}-2 minecraft:cobweb",
                "setblock {x}+2 {y}-10 {z}+2 minecraft:cobweb",
                # Treasure at bottom as bait next time
                "setblock {x} {y}-19 {z} minecraft:chest",
                # Pressure plate trigger on surface
                "setblock {x} {y}+1 {z}-1 minecraft:stone_pressure_plate",
            ],
        },
        {
            "name": "Chaos Tower",
            "announce": "Loki raises a tower of pure architectural insanity!",
            "subterranean": False,
            "commands": [
                # Base of random blocks
                "fill {x}-2 {y} {z}-2 {x}+2 {y} {z}+2 minecraft:sponge",
                # Each floor is a different crazy material
                "fill {x}-2 {y}+1 {z}-2 {x}+2 {y}+3 {z}+2 minecraft:pink_wool hollow",
                "fill {x}-2 {y}+4 {z}-2 {x}+2 {y}+6 {z}+2 minecraft:lime_concrete hollow",
                "fill {x}-2 {y}+7 {z}-2 {x}+2 {y}+9 {z}+2 minecraft:blue_ice hollow",
                "fill {x}-2 {y}+10 {z}-2 {x}+2 {y}+12 {z}+2 minecraft:magenta_glazed_terracotta hollow",
                "fill {x}-2 {y}+13 {z}-2 {x}+2 {y}+15 {z}+2 minecraft:honeycomb_block hollow",
                # Random protrusions
                "fill {x}+3 {y}+5 {z} {x}+5 {y}+7 {z} minecraft:slime_block",
                "fill {x}-5 {y}+9 {z} {x}-3 {y}+11 {z} minecraft:hay_block",
                "fill {x} {y}+12 {z}+3 {x} {y}+14 {z}+5 minecraft:melon",
                # Top: a cake
                "setblock {x} {y}+16 {z} minecraft:cake",
                # TNT hidden inside (Fuse:32767 = won't go off... unless someone lights it)
                "setblock {x} {y}+8 {z} minecraft:tnt",
                # Pigs
                "execute positioned {x} {y}+2 {z} run summon minecraft:pig",
                "execute positioned {x} {y}+5 {z} run summon minecraft:pig",
            ],
        },
        {
            "name": "The Labyrinth of Lies",
            "announce": "Loki digs a maze beneath the earth... good luck getting out!",
            "subterranean": True,
            "commands": [
                # Dig out a large underground area
                "fill {x}-8 {y}-6 {z}-8 {x}+8 {y}-2 {z}+8 minecraft:air",
                "fill {x}-9 {y}-7 {z}-9 {x}+9 {y}-1 {z}+9 minecraft:smooth_stone hollow",
                "fill {x}-8 {y}-6 {z}-8 {x}+8 {y}-2 {z}+8 minecraft:air",
                # Maze walls (simplified grid)
                "fill {x}-6 {y}-6 {z}-6 {x}-6 {y}-3 {z}+6 minecraft:mossy_cobblestone",
                "fill {x}-2 {y}-6 {z}-6 {x}-2 {y}-3 {z}+2 minecraft:mossy_cobblestone",
                "fill {x}+2 {y}-6 {z}-2 {x}+2 {y}-3 {z}+6 minecraft:mossy_cobblestone",
                "fill {x}+6 {y}-6 {z}-6 {x}+6 {y}-3 {z}+6 minecraft:mossy_cobblestone",
                "fill {x}-6 {y}-6 {z}+2 {x}+2 {y}-3 {z}+2 minecraft:mossy_cobblestone",
                "fill {x}-2 {y}-6 {z}-2 {x}+6 {y}-3 {z}-2 minecraft:mossy_cobblestone",
                # Dead ends with cobweb
                "setblock {x}-5 {y}-5 {z}+5 minecraft:cobweb",
                "setblock {x}+5 {y}-5 {z}-5 minecraft:cobweb",
                # Misleading signs
                "setblock {x}-4 {y}-5 {z}-6 minecraft:oak_wall_sign[facing=south]",
                "setblock {x}+4 {y}-5 {z}+6 minecraft:oak_wall_sign[facing=north]",
                # Prize in the center
                "setblock {x} {y}-6 {z} minecraft:jukebox",
                # Entrance
                "fill {x} {y}-1 {z}-9 {x} {y} {z}-9 minecraft:air",
                "fill {x} {y}-6 {z}-9 {x} {y}-2 {z}-9 minecraft:ladder[facing=south]",
            ],
        },
    ]

    # Collaborative structure blueprints — built when 2+ NPCs cooperate
    _COLLAB_STRUCTURES = [
        {
            "name": "Pantheon of the Gods",
            "announce": "The gods join forces to raise a Pantheon!",
            "subterranean": False,
            "commands": [
                # Massive foundation
                "fill {x}-8 {y} {z}-8 {x}+8 {y} {z}+8 minecraft:smooth_stone",
                "fill {x}-7 {y}+1 {z}-7 {x}+7 {y}+1 {z}+7 minecraft:polished_andesite",
                # Outer columns
                "fill {x}-7 {y}+2 {z}-7 {x}-7 {y}+10 {z}-7 minecraft:quartz_pillar",
                "fill {x}+7 {y}+2 {z}-7 {x}+7 {y}+10 {z}-7 minecraft:quartz_pillar",
                "fill {x}-7 {y}+2 {z}+7 {x}-7 {y}+10 {z}+7 minecraft:quartz_pillar",
                "fill {x}+7 {y}+2 {z}+7 {x}+7 {y}+10 {z}+7 minecraft:quartz_pillar",
                "fill {x}-7 {y}+2 {z} {x}-7 {y}+10 {z} minecraft:quartz_pillar",
                "fill {x}+7 {y}+2 {z} {x}+7 {y}+10 {z} minecraft:quartz_pillar",
                "fill {x} {y}+2 {z}-7 {x} {y}+10 {z}-7 minecraft:quartz_pillar",
                "fill {x} {y}+2 {z}+7 {x} {y}+10 {z}+7 minecraft:quartz_pillar",
                # Roof
                "fill {x}-8 {y}+11 {z}-8 {x}+8 {y}+11 {z}+8 minecraft:smooth_quartz",
                "fill {x}-6 {y}+12 {z}-6 {x}+6 {y}+12 {z}+6 minecraft:smooth_quartz",
                "fill {x}-4 {y}+13 {z}-4 {x}+4 {y}+13 {z}+4 minecraft:smooth_quartz",
                "fill {x}-2 {y}+14 {z}-2 {x}+2 {y}+14 {z}+2 minecraft:gold_block",
                "setblock {x} {y}+15 {z} minecraft:beacon",
                # Three throne areas inside
                # Good throne (green + gold)
                "setblock {x}-4 {y}+2 {z}+4 minecraft:emerald_block",
                "setblock {x}-4 {y}+3 {z}+5 minecraft:gold_block",
                "setblock {x}-4 {y}+4 {z}+5 minecraft:sea_lantern",
                # Evil throne (black + red)
                "setblock {x}+4 {y}+2 {z}+4 minecraft:blackstone",
                "setblock {x}+4 {y}+3 {z}+5 minecraft:crying_obsidian",
                "setblock {x}+4 {y}+4 {z}+5 minecraft:soul_fire",
                # Loki throne (gold + chaotic)
                "setblock {x} {y}+2 {z}-4 minecraft:gold_block",
                "setblock {x} {y}+3 {z}-5 minecraft:sponge",
                "setblock {x} {y}+4 {z}-5 minecraft:jack_o_lantern",
                # Inner air
                "fill {x}-6 {y}+2 {z}-6 {x}+6 {y}+10 {z}+6 minecraft:air",
                # Entrance
                "fill {x}-2 {y}+2 {z}-7 {x}+2 {y}+5 {z}-7 minecraft:air",
            ],
        },
        {
            "name": "The Abyss Cathedral",
            "announce": "The gods burrow deep to build a cathedral beneath the world...",
            "subterranean": True,
            "commands": [
                # Massive underground cavern
                "fill {x}-10 {y}-20 {z}-10 {x}+10 {y}-4 {z}+10 minecraft:air",
                "fill {x}-11 {y}-21 {z}-11 {x}+11 {y}-3 {z}+11 minecraft:deepslate_bricks hollow",
                "fill {x}-10 {y}-20 {z}-10 {x}+10 {y}-4 {z}+10 minecraft:air",
                # Floor — mixed divine
                "fill {x}-10 {y}-21 {z}-10 {x}+10 {y}-21 {z}+10 minecraft:polished_blackstone",
                "fill {x}-3 {y}-21 {z}-3 {x}+3 {y}-21 {z}+3 minecraft:gold_block",
                # Central altar
                "fill {x}-1 {y}-20 {z}-1 {x}+1 {y}-18 {z}+1 minecraft:obsidian",
                "setblock {x} {y}-17 {z} minecraft:beacon",
                "setblock {x} {y}-16 {z} minecraft:end_rod[facing=up]",
                # Good wing (left)
                "fill {x}-9 {y}-20 {z}-2 {x}-7 {y}-16 {z}+2 minecraft:quartz_block",
                "fill {x}-8 {y}-19 {z}-1 {x}-8 {y}-17 {z}+1 minecraft:air",
                "setblock {x}-8 {y}-18 {z} minecraft:sea_lantern",
                # Evil wing (right)
                "fill {x}+7 {y}-20 {z}-2 {x}+9 {y}-16 {z}+2 minecraft:nether_bricks",
                "fill {x}+8 {y}-19 {z}-1 {x}+8 {y}-17 {z}+1 minecraft:air",
                "setblock {x}+8 {y}-18 {z} minecraft:soul_lantern",
                # Loki balcony (above)
                "fill {x}-3 {y}-10 {z}-8 {x}+3 {y}-10 {z}-6 minecraft:honeycomb_block",
                "fill {x}-2 {y}-9 {z}-7 {x}+2 {y}-9 {z}-7 minecraft:glass",
                # Grand staircase
                "fill {x} {y}-3 {z}-11 {x} {y} {z}-11 minecraft:air",
                "fill {x} {y}-20 {z}-11 {x} {y}-4 {z}-11 minecraft:ladder[facing=south]",
                # Chandeliers
                "fill {x}-4 {y}-5 {z}-4 {x}-4 {y}-5 {z}-4 minecraft:chain",
                "setblock {x}-4 {y}-6 {z}-4 minecraft:lantern",
                "fill {x}+4 {y}-5 {z}+4 {x}+4 {y}-5 {z}+4 minecraft:chain",
                "setblock {x}+4 {y}-6 {z}+4 minecraft:lantern",
                "fill {x} {y}-5 {z} {x} {y}-5 {z} minecraft:chain",
                "setblock {x} {y}-6 {z} minecraft:end_rod[facing=down]",
            ],
        },
    ]

    # ------------------------------------------------------------------ #
    # AI-Powered Creative Building                                         #
    # ------------------------------------------------------------------ #

    _BUILD_THEMES = [
        "a grand temple with pillars and a glowing altar",
        "an underground crypt with hidden chambers and soul lanterns",
        "a floating island tethered by chains to the ground",
        "a colossal statue of a deity holding a beacon",
        "a sprawling garden maze with fountains and flower beds",
        "a volcanic forge with lava channels and obsidian anvils",
        "a crystal cavern deep underground with amethyst and glow lichen",
        "a wizard tower spiraling into the clouds with enchanting rooms",
        "a bridge connecting two cliff faces over a river of lava",
        "an underwater dome made of glass with coral gardens inside",
        "a dark fortress with nether brick walls and wither skeleton guards",
        "a treehouse village spanning multiple giant oak trees",
        "a colosseum with tiered seating and a redstone-powered arena floor",
        "a throne room carved into a mountainside with gold and emerald",
        "a lighthouse on a rocky shore that beams light across the sea",
        "a mysterious portal archway made of crying obsidian and end stone",
        "a library of infinite knowledge with bookshelves floor to ceiling",
        "a necropolis — a city of the dead with tombs and eerie lights",
        "a sky palace among the clouds with quartz floors and glass domes",
        "a subterranean mushroom kingdom with huge fungi and mycelium paths",
        "a war memorial with obsidian obelisks and eternal flame",
        "a bathhouse with warm water pools, steam vents, and stone carvings",
        "a pirate ship frozen in ice at the edge of a glacier",
        "a dragon's lair with gold hoards and a massive nest",
        "a clocktower with intricate redstone mechanisms",
    ]

    def _ai_design_structure(self, theme=None, collab_partner=None):
        """Use the LLM to creatively design a Minecraft structure, returning a blueprint dict.
        
        If collab_partner is set, the two gods discuss and plan together first.
        Returns: {"name": str, "announce": str, "subterranean": bool, "commands": [str]}
        """
        import random, re

        if not theme:
            theme = random.choice(self._BUILD_THEMES)

        # If collaborating, have a short planning conversation first
        build_context = ""
        if collab_partner and collab_partner is not self:
            partner_name = collab_partner.npc_name
            my_name = self.npc_name

            _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}
            my_color = _mode_mc_colors.get(self.mode, "light_purple")
            partner_color = _mode_mc_colors.get(collab_partner.mode, "gold")

            # God 1 proposes the idea
            propose_prompt = (
                f"You are {my_name}, a {self.mode} god in Minecraft. "
                f"You want to build something AMAZING with {partner_name} ({collab_partner.mode} god). "
                f"The inspiration is: \"{theme}\"\n\n"
                f"Propose your creative vision to {partner_name}. What should it look like? "
                f"What materials? What mood? Add your personality — be passionate and specific. "
                f"2-3 sentences. No action tags."
            )
            proposal = self._ask_mlx("__build_plan__", propose_prompt)
            proposal_clean = self._strip_actions(proposal)[:300]

            # Announce proposal in-game
            try:
                rcon_cmd(
                    f'tellraw @a [{{"text":"[{my_name}] ","color":"{my_color}","bold":true}},'
                    f'{{"text":"{self._escape_json(proposal_clean[:200])}","color":"gray","italic":true}}]'
                )
            except Exception:
                pass

            ts = time.strftime("%H:%M:%S")
            self._chat_log.append({
                "type": "god_chat",
                "text": proposal_clean[:300],
                "speaker": my_name, "listener": partner_name,
                "time": ts,
            })
            time.sleep(1.5)

            # God 2 responds with additions/modifications
            respond_prompt = (
                f"You are {partner_name}, a {collab_partner.mode} god in Minecraft. "
                f"{my_name} just proposed building something together:\n"
                f"\"{proposal_clean}\"\n\n"
                f"React to their idea! Add your own twist — what would YOU change or add? "
                f"Merge your personality into the design. Be creative and specific. "
                f"2-3 sentences. No action tags."
            )
            response = collab_partner._ask_mlx("__build_plan__", respond_prompt)
            response_clean = collab_partner._strip_actions(response)[:300]

            # Announce response in-game
            try:
                rcon_cmd(
                    f'tellraw @a [{{"text":"[{partner_name}] ","color":"{partner_color}","bold":true}},'
                    f'{{"text":"{collab_partner._escape_json(response_clean[:200])}","color":"gray","italic":true}}]'
                )
            except Exception:
                pass

            ts = time.strftime("%H:%M:%S")
            collab_partner._chat_log.append({
                "type": "god_chat",
                "text": response_clean[:300],
                "speaker": partner_name, "listener": my_name,
                "time": ts,
            })
            time.sleep(1.0)

            build_context = (
                f"{my_name} proposed: {proposal_clean}\n"
                f"{partner_name} added: {response_clean}\n"
            )

        # Now generate actual Minecraft build commands from the concept
        gen_prompt = (
            "You are a Minecraft build architect. Generate a creative structure as a list of "
            "Minecraft fill/setblock commands.\n\n"
        )
        if build_context:
            gen_prompt += f"Two gods discussed this build:\n{build_context}\n"
        else:
            gen_prompt += f"A {self.mode} god wants to build: \"{theme}\"\n"

        gen_prompt += (
            "\nRULES:\n"
            "1. Use coordinates relative to {x}, {y}, {z} (the build site center)\n"
            "2. Use {x}+N, {x}-N, {y}+N, {y}-N, {z}+N, {z}-N for offsets\n"
            "3. Only use: fill, setblock commands\n"
            "4. Use real Minecraft block IDs (minecraft:stone, minecraft:oak_planks, etc.)\n"
            "5. Build within a 20-block radius. Can go 25 blocks up or 20 blocks down.\n"
            "6. Be creative with materials — mix blocks for texture and depth\n"
            "7. Include interior detail: torches, lanterns, carpets, flower pots, etc.\n"
            "8. Clear interior air spaces after building walls\n\n"
            "FORMAT: Output ONLY the commands, one per line. Start each with 'fill' or 'setblock'.\n"
            "First line must be: NAME: <structure name>\n"
            "Second line must be: UNDERGROUND: yes OR UNDERGROUND: no\n"
            "Then the commands.\n\n"
            "Example:\n"
            "NAME: Crystal Spire\n"
            "UNDERGROUND: no\n"
            "fill {x}-3 {y} {z}-3 {x}+3 {y} {z}+3 minecraft:smooth_stone\n"
            "fill {x}-1 {y}+1 {z}-1 {x}+1 {y}+12 {z}+1 minecraft:amethyst_block\n"
            "setblock {x} {y}+13 {z} minecraft:beacon\n\n"
            "Generate 15-30 commands for a detailed, impressive structure."
        )

        # Direct LLM call with higher token limit for build commands
        try:
            _build_payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You are a Minecraft build architect. Output ONLY structured build commands."},
                    {"role": "user", "content": gen_prompt},
                ],
                "max_tokens": 600,
                "temperature": 0.85,
                "repetition_penalty": 1.3,
            }
            _build_resp = requests.post(
                f"http://{MLX_HOST}/v1/chat/completions",
                json=_build_payload,
                timeout=90,
            )
            _build_resp.raise_for_status()
            raw = _build_resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            import logging
            logging.warning(f"[{self.npc_name}] Build design failed: {type(e).__name__}: {e}")
            raw = ""

        # Parse the response into a blueprint
        lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
        name = theme[:40]
        subterranean = False
        commands = []

        for line in lines:
            if line.upper().startswith("NAME:"):
                name = line[5:].strip().strip('"').strip("'")[:60]
            elif line.upper().startswith("UNDERGROUND:"):
                val = line.split(":", 1)[1].strip().lower()
                subterranean = val in ("yes", "true", "1")
            elif line.startswith("fill ") or line.startswith("setblock "):
                # Validate it looks like a real MC command
                if "{x}" in line or "{y}" in line or "{z}" in line:
                    commands.append(line)

        # If AI didn't produce enough commands, add a basic platform + column as fallback
        if len(commands) < 5:
            commands = [
                "fill {x}-5 {y} {z}-5 {x}+5 {y} {z}+5 minecraft:smooth_stone",
                "fill {x}-4 {y}+1 {z}-4 {x}+4 {y}+1 {z}+4 minecraft:polished_andesite",
                "fill {x}-1 {y}+1 {z}-1 {x}+1 {y}+8 {z}+1 minecraft:quartz_pillar",
                "fill {x}-3 {y}+9 {z}-3 {x}+3 {y}+9 {z}+3 minecraft:smooth_quartz",
                "setblock {x} {y}+10 {z} minecraft:beacon",
                "setblock {x}+2 {y}+2 {z}+2 minecraft:lantern",
                "setblock {x}-2 {y}+2 {z}-2 minecraft:lantern",
            ]

        blueprint = {
            "name": name,
            "announce": f"{self.npc_name} begins to manifest: {name}!",
            "subterranean": subterranean,
            "commands": commands,
        }

        return blueprint

    def _ai_solo_build(self):
        """God creatively designs and builds a structure using AI imagination."""
        import random

        theme = random.choice(self._BUILD_THEMES)

        # Personality-flavored theme selection
        if self.mode == "good":
            flavor = random.choice(["sacred", "healing", "beautiful", "protective", "peaceful"])
            theme = f"a {flavor} " + theme.split(" ", 2)[-1] if len(theme.split(" ", 2)) > 2 else theme
        elif self.mode == "evil":
            flavor = random.choice(["dark", "corrupted", "terrifying", "apocalyptic", "cursed"])
            theme = f"a {flavor} " + theme.split(" ", 2)[-1] if len(theme.split(" ", 2)) > 2 else theme
        elif self.mode == "loki":
            flavor = random.choice(["absurd", "impossible", "chaotic", "mind-bending", "paradoxical"])
            theme = f"a {flavor} " + theme.split(" ", 2)[-1] if len(theme.split(" ", 2)) > 2 else theme

        # Think out loud about what to build
        think_prompt = (
            f"You are {self.npc_name}, a {self.mode} god in Minecraft. "
            f"You've decided to build something. The inspiration is: \"{theme}\"\n"
            f"Announce what you're about to create to the world. Be dramatic and excited. "
            f"1-2 sentences. No action tags."
        )
        announcement = self._ask_mlx("__build_think__", think_prompt)
        announcement_clean = self._strip_actions(announcement)[:250]

        _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}
        my_color = _mode_mc_colors.get(self.mode, "light_purple")

        try:
            rcon_cmd(
                f'tellraw @a [{{"text":"[{self.npc_name}] ","color":"{my_color}","bold":true}},'
                f'{{"text":"{self._escape_json(announcement_clean[:200])}","color":"gray","italic":true}}]'
            )
        except Exception:
            pass

        ts = time.strftime("%H:%M:%S")
        self._chat_log.append({
            "type": "god_chat",
            "text": f"[BUILD IDEA] {announcement_clean}",
            "speaker": self.npc_name, "listener": "world",
            "time": ts,
        })

        # Generate and execute
        blueprint = self._ai_design_structure(theme=theme)
        self._build_structure(blueprint)

    def _pick_build_site(self):
        """Choose a location to build near the entity. Returns (x, y, z)."""
        import random
        cur = self._get_entity_pos()
        if not cur:
            return int(self.home_x), int(self._entity_y), int(self.home_z)
        ox, oy, oz = int(cur[0]), int(cur[1]), int(cur[2])
        # Offset 20-60 blocks away so we don't overwrite player stuff
        dx = random.randint(20, 60) * random.choice([-1, 1])
        dz = random.randint(20, 60) * random.choice([-1, 1])
        return ox + dx, oy, oz + dz

    def _build_structure(self, blueprint, site_x=None, site_y=None, site_z=None):
        """Execute a structure blueprint at the given coordinates with dramatic flair."""
        import random

        bx, by, bz = site_x, site_y, site_z
        if bx is None:
            bx, by, bz = self._pick_build_site()

        name = blueprint["name"]
        announce = blueprint["announce"]
        commands = blueprint["commands"]

        # Walk to build site
        self._walk_to_player_pos(bx, by, bz, self.home_dimension)

        # Announce
        self._say("@a", announce)
        time.sleep(0.5)

        pre = self._dim_prefix()
        tag = self._entity_tag

        import random

        # Solo mid-build commentary
        _solo_comments = {
            "good": [
                "Yes... the energy flows through every block.",
                "This will be a sanctuary for the weary.",
                "Beauty takes form. The world approves.",
                "Light and stone, working in harmony.",
            ],
            "evil": [
                "Rise, monument to darkness!",
                "The very ground trembles at my creation.",
                "No mortal could conceive such dread beauty.",
                "Another scar upon this world. Magnificent.",
            ],
            "loki": [
                "Hmm, needs more... unpredictability.",
                "Architecture is just organized chaos. I prefer the chaos part.",
                "Even I'm surprised by what I'm building. That's a good sign.",
                "Physics? Never heard of her.",
            ],
        }

        total_cmds = len(commands)
        chat_points = set()
        if total_cmds > 4:
            for frac in [0.33, 0.66]:
                chat_points.add(int(total_cmds * frac))

        _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}
        my_color = _mode_mc_colors.get(self.mode, "light_purple")

        # Execute build commands with dramatic pauses, particles, and commentary
        for i, cmd in enumerate(commands):
            if self._stop.is_set():
                return

            # Substitute coordinates — handle {x}+N, {x}-N patterns
            import re as _re
            def _sub_coord(match):
                base_var = match.group(1)
                base_val = {"x": bx, "y": by, "z": bz}[base_var]
                op = match.group(2)
                offset = int(match.group(3))
                if op == "+":
                    return str(base_val + offset)
                else:
                    return str(base_val - offset)

            resolved = _re.sub(r'\{([xyz])\}([+-])(\d+)', _sub_coord, cmd)
            resolved = resolved.replace("{x}", str(bx)).replace("{y}", str(by)).replace("{z}", str(bz))

            if resolved.startswith("execute positioned") or resolved.startswith("summon"):
                rcon_cmd(resolved)
            else:
                rcon_cmd(f"{pre}{resolved}")

            # Particle effects while building (every 3rd command)
            if i % 3 == 0:
                try:
                    rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:firework ~ ~1 ~ 0.3 0.3 0.3 0.02 5')
                except Exception:
                    pass

            # Mid-build commentary
            if i in chat_points:
                try:
                    comments = _solo_comments.get(self.mode, _solo_comments["good"])
                    comment = random.choice(comments)
                    rcon_cmd(
                        f'tellraw @a [{{"text":"[{self.npc_name}] ","color":"{my_color}","bold":true}},'
                        f'{{"text":"{self._escape_json(comment)}","color":"gray","italic":true}}]'
                    )
                    self._chat_log.append({
                        "type": "god_chat",
                        "text": comment,
                        "speaker": self.npc_name, "listener": "world",
                        "time": time.strftime("%H:%M:%S"),
                    })
                except Exception:
                    pass

            if "fill" in cmd:
                time.sleep(0.4)
            else:
                time.sleep(0.2)

        # Completion fanfare
        rcon_cmd(f'{pre}execute positioned {bx} {by}+5 {bz} run particle minecraft:flash ~ ~ ~ 0 0 0 0 1')
        rcon_cmd(f'{pre}execute positioned {bx} {by}+3 {bz} run particle minecraft:firework ~ ~ ~ 3 3 3 0.1 30')

        # AI-generated completion reaction
        try:
            react_prompt = (
                f"You are {self.npc_name}, a {self.mode} god. "
                f"You just finished building \"{name}\". "
                f"React to your completed creation — admire it, critique it, brag about it. "
                f"1 sentence. No action tags."
            )
            reaction = self._ask_mlx("__build_react__", react_prompt)
            reaction_clean = self._strip_actions(reaction)[:200]
            if reaction_clean and reaction_clean != "...":
                self._say("@a", reaction_clean)
        except Exception:
            self._say("@a", f"{self.npc_name} has completed: {name}!")

        self._chat_log.append({
            "type": "system",
            "text": f"[BUILD] {name} at ({bx}, {by}, {bz})",
            "time": time.strftime("%H:%M:%S"),
        })

        # Remember what we built
        sub = "underground" if blueprint.get("subterranean") else "surface"
        self._remember_build(name, f"{sub} at {bx}, {by}, {bz}")

    def _collab_build(self, other_npc, theme_override=None):
        """Two NPCs discuss what to build, then collaboratively create it using AI."""
        import random

        # Guard: never collaborate with yourself
        if other_npc is self or other_npc.npc_id == self.npc_id:
            return

        my_name = self.npc_name
        their_name = other_npc.npc_name

        # AI designs the structure through conversation between the two gods
        theme = theme_override or random.choice(self._BUILD_THEMES)
        blueprint = self._ai_design_structure(theme=theme, collab_partner=other_npc)

        bx, by, bz = self._pick_build_site()

        _mode_mc_colors = {"good": "green", "evil": "red", "loki": "yellow"}
        my_color = _mode_mc_colors.get(self.mode, "light_purple")
        their_color = _mode_mc_colors.get(other_npc.mode, "gold")

        # Both NPCs walk to the site
        self._walk_to_player_pos(bx, by, bz, self.home_dimension)
        other_npc._walk_to_player_pos(bx + 5, by, bz + 5, self.home_dimension)

        # Announce collaboration
        safe_name = self._escape_json(blueprint["name"])
        rcon_cmd(f'tellraw @a [{{"text":"[DIVINE CONSTRUCTION] ","color":"gold","bold":true}},'
                 f'{{"text":"{my_name}","color":"{my_color}"}},'
                 f'{{"text":" and ","color":"white"}},'
                 f'{{"text":"{their_name}","color":"{their_color}"}},'
                 f'{{"text":" begin creating: {safe_name}","color":"yellow"}}]')

        # Execute the build — alternate between builders
        commands = blueprint["commands"]
        pre = self._dim_prefix()

        import re as _re
        def _resolve(cmd, x, y, z):
            def _sub_coord(match):
                base_var = match.group(1)
                base_val = {"x": x, "y": y, "z": z}[base_var]
                op = match.group(2)
                offset = int(match.group(3))
                return str(base_val + offset if op == "+" else base_val - offset)
            resolved = _re.sub(r'\{([xyz])\}([+-])(\d+)', _sub_coord, cmd)
            return resolved.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(z))

        # Mid-build chatter — gods comment while constructing
        total_cmds = len(commands)
        chat_points = set()
        if total_cmds > 4:
            # Chat at ~25%, ~50%, ~75% through the build
            for frac in [0.25, 0.5, 0.75]:
                chat_points.add(int(total_cmds * frac))

        _build_comments = {
            "good": [
                "This foundation feels right... the land welcomes it.",
                "I can sense the harmony taking shape.",
                "Every block placed with purpose. This will endure.",
                "The light will find its way in. I'll make sure of it.",
            ],
            "evil": [
                "Yes... the shadows are deepening. Perfect.",
                "This structure will make them tremble.",
                "More obsidian. It needs to feel... oppressive.",
                "The darkness here is exquisite.",
            ],
            "loki": [
                "Wait, what if we made this part upside down?",
                "Haha, this is looking wonderfully chaotic!",
                "I'm adding a secret room. Don't tell anyone.",
                "This defies every law of architecture. I love it.",
            ],
        }

        for i, cmd in enumerate(commands):
            if self._stop.is_set():
                return

            resolved = _resolve(cmd, bx, by, bz)
            builder = self if i % 2 == 0 else other_npc

            if resolved.startswith("execute positioned") or resolved.startswith("summon"):
                rcon_cmd(resolved)
            else:
                rcon_cmd(f"{pre}{resolved}")

            # Alternate particle effects between builders
            try:
                btag = builder._entity_tag
                rcon_cmd(f'{pre}execute as @e[tag={btag}] at @s run particle minecraft:end_rod ~ ~1 ~ 0.3 0.3 0.3 0.02 3')
            except Exception:
                pass

            # Mid-build chatter at key points
            if i in chat_points:
                try:
                    talker = random.choice([self, other_npc])
                    comments = _build_comments.get(talker.mode, _build_comments["good"])
                    comment = random.choice(comments)
                    tc = _mode_mc_colors.get(talker.mode, "light_purple")
                    rcon_cmd(
                        f'tellraw @a [{{"text":"[{talker.npc_name}] ","color":"{tc}","bold":true}},'
                        f'{{"text":"{talker._escape_json(comment)}","color":"gray","italic":true}}]'
                    )
                    talker._chat_log.append({
                        "type": "god_chat",
                        "text": comment,
                        "speaker": talker.npc_name,
                        "listener": (self if talker is other_npc else other_npc).npc_name,
                        "time": time.strftime("%H:%M:%S"),
                    })
                except Exception:
                    pass

            time.sleep(0.35)

        # Grand completion effects
        rcon_cmd(f'{pre}execute positioned {bx} {by}+8 {bz} run particle minecraft:flash ~ ~ ~ 0 0 0 0 1')
        rcon_cmd(f'{pre}execute positioned {bx} {by}+5 {bz} run particle minecraft:firework ~ ~ ~ 5 5 5 0.1 50')
        rcon_cmd(f'{pre}playsound minecraft:ui.toast.challenge_complete master @a {bx} {by} {bz} 1 1')

        # AI-generated completion reactions (not generic)
        for reactor in [self, other_npc]:
            try:
                react_prompt = (
                    f"You are {reactor.npc_name}, a {reactor.mode} god. "
                    f"You just finished building \"{blueprint['name']}\" with {(other_npc if reactor is self else self).npc_name}. "
                    f"React to seeing the completed structure. Be genuine — proud, impressed, critical, or amused. "
                    f"1 sentence. No action tags."
                )
                reaction = reactor._ask_mlx("__build_react__", react_prompt)
                reaction_clean = reactor._strip_actions(reaction)[:200]
                if reaction_clean and reaction_clean != "...":
                    rc = _mode_mc_colors.get(reactor.mode, "light_purple")
                    rcon_cmd(
                        f'tellraw @a [{{"text":"[{reactor.npc_name}] ","color":"{rc}","bold":true}},'
                        f'{{"text":"{reactor._escape_json(reaction_clean)}","color":"gray","italic":true}}]'
                    )
                    reactor._chat_log.append({
                        "type": "god_chat",
                        "text": reaction_clean,
                        "speaker": reactor.npc_name,
                        "listener": (other_npc if reactor is self else self).npc_name,
                        "time": time.strftime("%H:%M:%S"),
                    })
                    time.sleep(1.0)
            except Exception:
                pass

        for npc in (self, other_npc):
            npc._chat_log.append({
                "type": "system",
                "text": f"[COLLAB BUILD] {blueprint['name']} at ({bx}, {by}, {bz})",
                "time": time.strftime("%H:%M:%S"),
            })

        # Both gods remember building together
        self._remember_build(f"{blueprint['name']} (with {their_name})", f"at {bx}, {by}, {bz}")
        other_npc._remember_build(f"{blueprint['name']} (with {my_name})", f"at {bx}, {by}, {bz}")

    def move_entity(self, x, z, dim=None):
        dim = dim or self.home_dimension
        self.home_x = float(x)
        self.home_z = float(z)
        self.home_dimension = dim
        if self.running:
            cur = self._get_entity_pos()
            y = cur[1] if cur else 100
            self._tp_entity(self.home_x, y, self.home_z, dim)

    # ------------------------------------------------------------------ #
    # ACTION ENGINE — parse LLM action tags and execute                    #
    # ------------------------------------------------------------------ #
    _ACTION_RE = re.compile(r'\[(\w+)\s*(.*?)\]')

    # Names the LLM may hallucinate instead of using the real player name
    _PLACEHOLDER_NAMES = {
        "player", "the_player", "<player>", "playername", "target",
        "username", "user", "them", "mortal", "adventurer",
    }

    def _resolve_player(self, name, default):
        """Replace LLM placeholder names with the actual player name."""
        if not name or name.lower().strip("<>") in self._PLACEHOLDER_NAMES:
            return default
        return name

    def _execute_actions(self, text, default_player="@a"):
        """Parse and execute action tags from LLM response."""
        actions = self._ACTION_RE.findall(text)
        pre = self._dim_prefix()
        for action, args in actions:
            action = action.upper()
            parts = args.strip().split()

            # Check ability toggles — skip disabled actions
            if not self._is_action_allowed(action):
                self._chat_log.append({
                    "type": "system",
                    "text": f"[BLOCKED] {action} — ability disabled for {self.npc_name}",
                    "time": time.strftime("%H:%M:%S"),
                })
                continue

            try:
                if action == "GIVE" and len(parts) >= 2:
                    player = self._resolve_player(parts[0], default_player)
                    item = parts[1]
                    count = parts[2] if len(parts) >= 3 else "1"
                    # Strip minecraft: prefix if LLM included it
                    item = item.replace("minecraft:", "")
                    rcon_cmd(f"give {player} minecraft:{item} {count}")
                elif action == "HEAL":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"effect give {player} minecraft:instant_health 1 5")
                    rcon_cmd(f"effect give {player} minecraft:saturation 10 5")
                elif action == "LIGHTNING":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"execute at {player} run summon minecraft:lightning_bolt")
                elif action == "TNT":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"execute at {player} run summon minecraft:tnt ~2 ~1 ~2")
                elif action == "MOB" and len(parts) >= 2:
                    player = self._resolve_player(parts[0], default_player)
                    mob = parts[1]
                    count = int(parts[2]) if len(parts) >= 3 else 1
                    for _ in range(min(count, 10)):
                        rcon_cmd(f"execute at {player} run summon minecraft:{mob} ~{__import__('random').randint(-3,3)} ~ ~{__import__('random').randint(-3,3)}")
                elif action == "EFFECT" and len(parts) >= 2:
                    player = self._resolve_player(parts[0], default_player)
                    effect = parts[1]
                    secs = parts[2] if len(parts) >= 3 else "10"
                    rcon_cmd(f"effect give {player} minecraft:{effect} {secs} 1")
                elif action == "WEATHER" and len(parts) >= 1:
                    duration = parts[1] if len(parts) >= 2 else ""
                    rcon_cmd(f"weather {parts[0]} {duration}".strip())
                elif action == "TIME" and len(parts) >= 1:
                    if len(parts) >= 2:
                        rcon_cmd(f"time {parts[0]} {parts[1]}")
                    else:
                        rcon_cmd(f"time set {parts[0]}")
                elif action == "LAVA":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"execute at {player} run setblock ~1 ~2 ~ minecraft:lava")
                elif action == "CLEAR_INV":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"clear {player}")
                elif action == "TP_RANDOM":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"spreadplayers ~ ~ 50 200 false {player}")
                elif action == "TP_SELF":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    self._walk_to_player(player)
                elif action == "SWAP_POS" and len(parts) >= 2:
                    p1 = self._resolve_player(parts[0], default_player)
                    p2 = self._resolve_player(parts[1], default_player)
                    rcon_cmd(f"execute as {p1} at {p2} run tp {p1} ~ ~ ~")
                    rcon_cmd(f"execute as {p2} at {p1} run tp {p2} ~ ~ ~")

                # ---- ENVIRONMENTAL / GOD-MODE ACTIONS ---- #

                elif action == "PLACE" and len(parts) >= 1:
                    # Place a block near/at player: [PLACE block] or [PLACE player block] or [PLACE block x y z]
                    player = default_player
                    block = parts[0].replace("minecraft:", "")
                    if len(parts) >= 2 and not parts[1].lstrip("-").isdigit():
                        player = self._resolve_player(parts[0], default_player)
                        block = parts[1].replace("minecraft:", "")
                    if len(parts) >= 4 and parts[-3].lstrip("-").isdigit():
                        x, y, z = parts[-3], parts[-2], parts[-1]
                        rcon_cmd(f"{pre}setblock {x} {y} {z} minecraft:{block}")
                    else:
                        rcon_cmd(f"execute at {player} run setblock ~1 ~ ~1 minecraft:{block}")
                elif action == "FILL" and len(parts) >= 1:
                    # Fill area near player: [FILL player block radius] or [FILL block radius]
                    player = default_player
                    block = parts[0].replace("minecraft:", "")
                    r = 3
                    if len(parts) >= 3:
                        player = self._resolve_player(parts[0], default_player)
                        block = parts[1].replace("minecraft:", "")
                        r = min(int(parts[2]), 10)
                    elif len(parts) >= 2:
                        r = min(int(parts[1]), 10) if parts[1].isdigit() else 3
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~ ~-{r} ~{r} ~1 ~{r} minecraft:{block}")
                elif action == "DESTROY":
                    # Destroy area around player: [DESTROY player radius] or [DESTROY player]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    r = min(int(parts[1]), 15) if len(parts) >= 2 and parts[1].isdigit() else 5
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~-1 ~-{r} ~{r} ~{r} ~{r} minecraft:air")
                elif action == "WALL" and len(parts) >= 1:
                    # Build wall around player: [WALL player block] or [WALL player]
                    player = self._resolve_player(parts[0], default_player)
                    block = parts[1].replace("minecraft:", "") if len(parts) >= 2 else "obsidian"
                    r = 3
                    # Four walls
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~ ~-{r} ~{r} ~3 ~-{r} minecraft:{block}")
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~ ~{r} ~{r} ~3 ~{r} minecraft:{block}")
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~ ~-{r} ~-{r} ~3 ~{r} minecraft:{block}")
                    rcon_cmd(f"execute at {player} run fill ~{r} ~ ~-{r} ~{r} ~3 ~{r} minecraft:{block}")
                elif action == "CAGE":
                    # Cage player in blocks: [CAGE player block]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    block = parts[1].replace("minecraft:", "") if len(parts) >= 2 else "obsidian"
                    # 3x3x3 hollow box
                    rcon_cmd(f"execute at {player} run fill ~-1 ~-1 ~-1 ~1 ~2 ~1 minecraft:{block}")
                    rcon_cmd(f"execute at {player} run fill ~ ~ ~ ~ ~1 ~ minecraft:air")
                elif action == "TREE":
                    # Grow a tree near player: [TREE player]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"execute at {player} run setblock ~2 ~ ~2 minecraft:oak_sapling")
                    rcon_cmd(f"execute at {player} run setblock ~2 ~1 ~2 minecraft:bone_block")
                    # Force grow
                    for _ in range(5):
                        rcon_cmd(f"execute at {player} run setblock ~2 ~ ~2 minecraft:oak_sapling")
                elif action == "EXPLODE":
                    # Big explosion near player: [EXPLODE player power]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    power = min(int(parts[1]), 10) if len(parts) >= 2 and parts[1].isdigit() else 4
                    rcon_cmd(f"execute at {player} run summon minecraft:tnt ~3 ~1 ~3 {{Fuse:0}}")
                    if power > 4:
                        for i in range(min(power - 3, 5)):
                            rcon_cmd(f"execute at {player} run summon minecraft:tnt ~{i-2} ~1 ~{i-2} {{Fuse:{i*2}}}")
                elif action == "FLOOD":
                    # Water flood around player: [FLOOD player radius]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    r = min(int(parts[1]), 8) if len(parts) >= 2 and parts[1].isdigit() else 4
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~1 ~-{r} ~{r} ~1 ~{r} minecraft:water")
                elif action == "FIRE":
                    # Set area on fire: [FIRE player radius]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    r = min(int(parts[1]), 6) if len(parts) >= 2 and parts[1].isdigit() else 3
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~ ~-{r} ~{r} ~ ~{r} minecraft:fire")
                elif action == "SMITE":
                    # Multi-lightning strike: [SMITE player count]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    count = min(int(parts[1]), 10) if len(parts) >= 2 and parts[1].isdigit() else 5
                    for i in range(count):
                        rcon_cmd(f"execute at {player} run summon minecraft:lightning_bolt ~{__import__('random').randint(-3,3)} ~ ~{__import__('random').randint(-3,3)}")
                        time.sleep(0.2)
                elif action == "BIOME_FX":
                    # Ambient particle effects: [BIOME_FX type] — rain particles, snow, etc
                    fx = parts[0] if parts else "enchant"
                    player = self._resolve_player(parts[1] if len(parts) >= 2 else None, default_player)
                    for _ in range(8):
                        rcon_cmd(f"execute at {player} run particle minecraft:{fx} ~{__import__('random').randint(-10,10)} ~{__import__('random').randint(1,8)} ~{__import__('random').randint(-10,10)} 2 2 2 0.05 20")
                elif action == "ENCHANT_AREA":
                    # Make an area sparkle with enchantment particles: [ENCHANT_AREA player]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    for _ in range(5):
                        rcon_cmd(f"execute at {player} run particle minecraft:enchant ~ ~1 ~ 5 3 5 1 100")
                        rcon_cmd(f"execute at {player} run particle minecraft:end_rod ~ ~2 ~ 5 3 5 0.02 30")
                elif action == "FREEZE":
                    # Freeze player area in ice/snow: [FREEZE player radius]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    r = min(int(parts[1]), 8) if len(parts) >= 2 and parts[1].isdigit() else 4
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~-1 ~-{r} ~{r} ~-1 ~{r} minecraft:ice")
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~3 ~-{r} ~{r} ~3 ~{r} minecraft:snow_block")
                    rcon_cmd(f"execute at {player} run particle minecraft:snowflake ~ ~2 ~ {r} 3 {r} 0.05 100")
                    rcon_cmd(f"effect give {player} minecraft:slowness 10 3")
                elif action == "GARDEN":
                    # Grow flowers and grass: [GARDEN player radius]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    r = min(int(parts[1]), 8) if len(parts) >= 2 and parts[1].isdigit() else 5
                    flowers = ["dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
                               "red_tulip", "orange_tulip", "pink_tulip", "cornflower", "lily_of_the_valley"]
                    rcon_cmd(f"execute at {player} run fill ~-{r} ~-1 ~-{r} ~{r} ~-1 ~{r} minecraft:grass_block")
                    for _ in range(10):
                        flower = __import__('random').choice(flowers)
                        fx = __import__('random').randint(-r, r)
                        fz = __import__('random').randint(-r, r)
                        rcon_cmd(f"execute at {player} run setblock ~{fx} ~ ~{fz} minecraft:{flower}")
                    rcon_cmd(f"execute at {player} run particle minecraft:happy_villager ~ ~1 ~ {r} 2 {r} 0.1 50")
                elif action == "TOWER":
                    # Build a tower under player: [TOWER player height block]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    height = min(int(parts[1]), 30) if len(parts) >= 2 and parts[1].isdigit() else 10
                    block = parts[2].replace("minecraft:", "") if len(parts) >= 3 else "stone"
                    rcon_cmd(f"execute at {player} run fill ~ ~ ~ ~ ~{height} ~ minecraft:{block}")
                    rcon_cmd(f"tp {player} ~ ~{height+1} ~")
                elif action == "PIT":
                    # Dig a pit under player: [PIT player depth]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    depth = min(int(parts[1]), 20) if len(parts) >= 2 and parts[1].isdigit() else 8
                    rcon_cmd(f"execute at {player} run fill ~-1 ~-1 ~-1 ~1 ~-{depth} ~1 minecraft:air")
                elif action == "RAIN_ITEMS" and len(parts) >= 1:
                    # Rain items from above: [RAIN_ITEMS player item count]
                    player = self._resolve_player(parts[0], default_player)
                    item = parts[1].replace("minecraft:", "") if len(parts) >= 2 else "diamond"
                    count = min(int(parts[2]), 20) if len(parts) >= 3 and parts[2].isdigit() else 5
                    for i in range(count):
                        ox = __import__('random').randint(-3, 3)
                        oz = __import__('random').randint(-3, 3)
                        rcon_cmd(f"execute at {player} run summon minecraft:item ~{ox} ~8 ~{oz} {{Item:{{id:\"minecraft:{item}\",Count:1}}}}")
                elif action == "FIREWORKS":
                    # Launch fireworks: [FIREWORKS player count]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    count = min(int(parts[1]), 15) if len(parts) >= 2 and parts[1].isdigit() else 5
                    colors = ["1973019", "11743532", "3887386", "16738740", "4408131"]
                    for i in range(count):
                        c = __import__('random').choice(colors)
                        ox = __import__('random').randint(-5, 5)
                        oz = __import__('random').randint(-5, 5)
                        rcon_cmd(f'execute at {player} run summon minecraft:firework_rocket ~{ox} ~1 ~{oz} '
                                 f'{{LifeTime:20,FireworksItem:{{id:"minecraft:firework_rocket",Count:1,components:{{"minecraft:fireworks":{{flight_duration:2,explosions:[{{shape:"large_ball",colors:[I;{c}],has_trail:true}}]}}}}}}}}')
                elif action == "GAMEMODE" and len(parts) >= 1:
                    # Change player gamemode: [GAMEMODE player mode]
                    player = self._resolve_player(parts[0], default_player)
                    mode = parts[1] if len(parts) >= 2 else "survival"
                    if mode in ("survival", "creative", "adventure", "spectator"):
                        rcon_cmd(f"gamemode {mode} {player}")
                elif action == "XP" and len(parts) >= 1:
                    # Give XP: [XP player amount]
                    player = self._resolve_player(parts[0], default_player)
                    amount = parts[1] if len(parts) >= 2 else "100"
                    rcon_cmd(f"xp add {player} {amount}")
                elif action == "SOUND" and len(parts) >= 1:
                    # Play sound: [SOUND sound_name]
                    sound = parts[0]
                    player = self._resolve_player(parts[1] if len(parts) >= 2 else None, default_player)
                    rcon_cmd(f"execute at {player} run playsound minecraft:{sound} master @a ~ ~ ~ 1 1")
                elif action == "TITLE" and len(parts) >= 1:
                    # Show title on screen: [TITLE player message]
                    player = self._resolve_player(parts[0], default_player)
                    msg = " ".join(parts[1:]) if len(parts) >= 2 else "The Oracle speaks!"
                    safe_msg = msg.replace('"', '\\"')
                    rcon_cmd(f'title {player} title {{"text":"{safe_msg}","color":"gold","bold":true}}')
                elif action == "CMD" and parts:
                    # Raw command execution (Loki god-mode): [CMD raw_command_here]
                    raw = " ".join(parts)
                    rcon_cmd(raw)

                # ---- ADMIN POWER ACTIONS ---- #

                elif action == "KICK":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    reason = " ".join(parts[1:]) if len(parts) >= 2 else "Kicked by the gods"
                    rcon_cmd(f'kick {player} {reason}')
                elif action == "BAN":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    reason = " ".join(parts[1:]) if len(parts) >= 2 else "Banned by divine decree"
                    rcon_cmd(f'ban {player} {reason}')
                elif action == "UNBAN":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f'pardon {player}')
                elif action == "OP":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f'op {player}')
                elif action == "DEOP":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f'deop {player}')
                elif action == "WHITELIST_ADD":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f'whitelist add {player}')
                elif action == "WHITELIST_REMOVE":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f'whitelist remove {player}')
                elif action == "MSG":
                    # Private message to a player: [MSG player message]
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    msg = " ".join(parts[1:]) if len(parts) >= 2 else "The gods see all."
                    safe = msg.replace('"', '\\"')
                    rcon_cmd(f'tellraw {player} [{{"text":"[{self.npc_name}] ","color":"{self.color}","bold":true}},'
                             f'{{"text":"{safe}","color":"white","italic":true}}]')
                elif action == "MUTE":
                    # Mute via slowness + blindness (no chat mute in vanilla, so we debuff)
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    secs = parts[1] if len(parts) >= 2 and parts[1].isdigit() else "30"
                    rcon_cmd(f"effect give {player} minecraft:slowness {secs} 100")
                    rcon_cmd(f"effect give {player} minecraft:blindness {secs} 0")
                    self._say("@a", f"{player} has been silenced by {self.npc_name}!")
                elif action == "UNMUTE":
                    player = self._resolve_player(parts[0] if parts else None, default_player)
                    rcon_cmd(f"effect clear {player} minecraft:slowness")
                    rcon_cmd(f"effect clear {player} minecraft:blindness")
                    self._say("@a", f"{player} has been freed by {self.npc_name}.")

                elif action == "FLY":
                    import random as _rng
                    cur = self._get_entity_pos()
                    if cur:
                        tag = self._entity_tag
                        ox, oy, oz = cur
                        self._say("@a", "Catch me if you can!")
                        # Dramatic ascent with particle trail
                        for i in range(10):
                            ny = oy + (i * 6)
                            self._tp_entity(ox, ny, oz)
                            rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:firework ~ ~ ~ 0.3 0.3 0.3 0.05 10')
                            rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:end_rod ~ ~ ~ 0.5 0.5 0.5 0.02 5')
                            time.sleep(0.12)
                        # Big firework burst at peak
                        rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:flash ~ ~ ~ 0 0 0 0 1')
                        rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:firework ~ ~ ~ 1 1 1 0.1 40')
                        rcon_cmd(f'{pre}playsound minecraft:entity.firework_rocket.launch master @a')
                        time.sleep(0.5)
                        # Teleport to a random offset (fly AWAY)
                        dx = _rng.randint(-80, 80)
                        dz = _rng.randint(-80, 80)
                        new_x = ox + dx
                        new_z = oz + dz
                        self._tp_entity(new_x, 320, new_z)
                        time.sleep(0.3)
                        # Let gravity bring it down
                        rcon_cmd(f'{pre}data merge entity @e[tag={tag},limit=1] {{NoGravity:0b}}')
                        time.sleep(1.0)
                        landed = self._get_entity_pos()
                        if landed:
                            self._entity_y = landed[1]
                            self.home_x, self.home_z = landed[0], landed[2]
                        rcon_cmd(f'{pre}execute as @e[tag={tag}] at @s run particle minecraft:witch ~ ~1 ~ 0.5 0.5 0.5 0.02 15')
                        self._say("@a", "The Oracle has relocated. Good luck finding me!")

                self._chat_log.append({
                    "type": "system",
                    "text": f"[ACTION] {action} {args}",
                    "time": time.strftime("%H:%M:%S"),
                })
            except Exception as e:
                self._chat_log.append({
                    "type": "error",
                    "text": f"Action {action} failed: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })

    def _strip_actions(self, text):
        """Remove action tags from text for display in chat."""
        return self._ACTION_RE.sub('', text).strip()

    # ------------------------------------------------------------------ #
    # Public control                                                       #
    # ------------------------------------------------------------------ #
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._apply_mode()
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        try:
            self._spawn_entity()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        try:
            self._despawn_entity()
        except Exception:
            pass

    def get_status(self):
        mlx_ok = False
        try:
            r = requests.get(f"http://{MLX_HOST}/v1/models", timeout=2)
            mlx_ok = r.status_code == 200
        except Exception:
            pass
        live_x, live_y, live_z = None, None, None
        if self.running:
            try:
                pos = self._get_entity_pos()
                if pos:
                    live_x, live_y, live_z = pos[0], pos[1], pos[2]
            except Exception:
                pass
        return {
            "npc_id": self.npc_id,
            "running": self.running,
            "npc_name": self.npc_name,
            "trigger": self.trigger,
            "mode": self.mode,
            "personality": self.personality,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "repetition_penalty": self.repetition_penalty,
            "color": self.color,
            "auto_walk": self.auto_walk,
            "auto_roam": self.auto_roam,
            "roam_interval": self.roam_interval,
            "auto_chat": self.auto_chat,
            "auto_god_chat": self.auto_god_chat,
            "auto_build": self.auto_build,
            "auto_clash": self.auto_clash,
            "abilities": dict(self.abilities),
            "memory_size": len(self._memory_cache) if self._memory_cache else 0,
            "memory_path": self._memory_path,
            "mlx_connected": mlx_ok,
            "home_x": self.home_x,
            "home_z": self.home_z,
            "home_dimension": self.home_dimension,
            "live_x": live_x,
            "live_y": live_y,
            "live_z": live_z,
            "last_interact_player": self.last_interact_player,
            "last_interact_x": self.last_interact_x,
            "last_interact_z": self.last_interact_z,
            "last_interact_dim": self.last_interact_dim,
            "last_interact_time": self.last_interact_time,
            "chat_log": list(self._chat_log),
            "history_per_player": {
                k: list(v) for k, v in self._history.items()
            },
        }

    def update_config(self, data):
        if "npc_name" in data:
            self.npc_name = data["npc_name"]
        if "trigger" in data:
            self.trigger = data["trigger"]
        if "mode" in data:
            old_mode = self.mode
            self.mode = data["mode"]
            self._apply_mode()
            if self.running and old_mode != self.mode:
                try:
                    self._spawn_entity()  # Respawn with new color/effects
                except Exception:
                    pass
        if "personality" in data:
            self.personality = data["personality"]
        if "model" in data:
            self.model = data["model"]
        if "max_tokens" in data:
            self.max_tokens = int(data["max_tokens"])
        if "temperature" in data:
            self.temperature = float(data["temperature"])
        if "repetition_penalty" in data:
            self.repetition_penalty = float(data["repetition_penalty"])
        if "color" in data:
            self.color = data["color"]
        if "auto_walk" in data:
            self.auto_walk = bool(data["auto_walk"])
        if "auto_roam" in data:
            self.auto_roam = bool(data["auto_roam"])
            if self.auto_roam and self.running:
                self._start_roam_loop()
        if "auto_chat" in data:
            self.auto_chat = bool(data["auto_chat"])
        if "auto_god_chat" in data:
            self.auto_god_chat = bool(data["auto_god_chat"])
        if "auto_build" in data:
            self.auto_build = bool(data["auto_build"])
        if "auto_clash" in data:
            self.auto_clash = bool(data["auto_clash"])
        if "roam_interval" in data:
            self.roam_interval = max(5, int(data["roam_interval"]))
        if "abilities" in data:
            for key, val in data["abilities"].items():
                if key in self.abilities:
                    self.abilities[key] = bool(val)

        home_moved = False
        if "home_x" in data:
            self.home_x = float(data["home_x"]); home_moved = True
        if "home_z" in data:
            self.home_z = float(data["home_z"]); home_moved = True
        if "home_dimension" in data:
            self.home_dimension = data["home_dimension"]; home_moved = True
        if home_moved and self.running:
            try:
                self.move_entity(self.home_x, self.home_z, self.home_dimension)
            except Exception:
                pass

    def test_prompt(self, prompt, player="TestPlayer"):
        return self._ask_mlx(player, prompt)

    # ------------------------------------------------------------------ #
    # Internal — log tailer + chat processing                              #
    # ------------------------------------------------------------------ #
    def _run(self):
        self._chat_log.append({
            "type": "system",
            "text": f"NPC '{self.npc_name}' starting in {self.mode.upper()} mode, trigger: {self.trigger}",
            "time": time.strftime("%H:%M:%S"),
        })
        # Hide command feedback from player chat
        try:
            rcon_cmd("gamerule send_command_feedback false")
            rcon_cmd("gamerule command_block_output false")
            rcon_cmd("gamerule log_admin_commands false")
        except Exception:
            pass

        # Announce to players
        mode_label = self.MODES.get(self.mode, {}).get("label", self.mode)
        self._say("@a", f"I have awakened as the {mode_label}. Speak to me with {self.trigger}.")

        while not self._stop.is_set():
            if not os.path.exists(MC_LOG_PATH):
                self._stop.wait(2)
                continue
            try:
                with open(MC_LOG_PATH, "r") as f:
                    f.seek(0, 2)
                    last_pos = f.tell()
                    while not self._stop.is_set():
                        line = f.readline()
                        if not line:
                            try:
                                cur_size = os.path.getsize(MC_LOG_PATH)
                                if cur_size < last_pos:
                                    break
                            except OSError:
                                break
                            self._stop.wait(0.2)
                            continue
                        last_pos = f.tell()
                        self._process_line(line.strip())
            except Exception as e:
                self._chat_log.append({
                    "type": "error",
                    "text": f"Log tailer error: {e}",
                    "time": time.strftime("%H:%M:%S"),
                })
                self._stop.wait(3)

        self._chat_log.append({
            "type": "system",
            "text": f"NPC '{self.npc_name}' stopped.",
            "time": time.strftime("%H:%M:%S"),
        })
        self.running = False

    def _lookup_player_pos(self, player_name):
        try:
            pos_resp = rcon_cmd(f"data get entity {player_name} Pos")
            dim_resp = rcon_cmd(f"data get entity {player_name} Dimension")
            pos_match = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', pos_resp or "")
            dim_match = re.search(r'"minecraft:(\w+)"', dim_resp or "")
            if pos_match:
                self.last_interact_x = round(float(pos_match.group(1)), 1)
                self.last_interact_z = round(float(pos_match.group(3)), 1)
            if dim_match:
                self.last_interact_dim = dim_match.group(1)
            self.last_interact_player = player_name
            self.last_interact_time = time.strftime("%H:%M:%S")
        except Exception:
            pass

    def _process_line(self, line):
        m = _CHAT_RE.match(line)
        if not m:
            return
        player = m.group(1)
        message = m.group(2).strip()
        if not message:
            return

        is_triggered = message.lower().startswith(self.trigger.lower())

        if is_triggered:
            prompt = message[len(self.trigger):].strip() or "Hello?"
        elif self.auto_chat:
            # Auto-chat: respond to regular player chat
            # Skip very short messages (just greetings between players etc)
            if len(message) < 3:
                return
            # Build a contextual prompt — give Loki mode extra juice
            if self.mode == "loki":
                prompt = (
                    f"Player {player} just said: \"{message}\"\n"
                    f"React to EXACTLY what they said. Twist their words, trick them, "
                    f"or be cleverly deceptive. Reference their message directly."
                )
            elif self.mode == "evil":
                prompt = (
                    f"Player {player} said: \"{message}\"\n"
                    f"Respond menacingly and take a dark action related to what they said."
                )
            else:
                prompt = (
                    f"Player {player} said in chat: \"{message}\"\n"
                    f"Respond helpfully and kindly to what they said."
                )
        else:
            return

        self._chat_log.append({
            "type": "player",
            "player": player,
            "text": message if not is_triggered else prompt,
            "time": time.strftime("%H:%M:%S"),
        })

        # Walk + lookup in background while LLM thinks (parallel)
        walk_thread = None
        if self.auto_walk and (is_triggered or self.npc_name.lower() in message.lower()):
            def _walk_and_prep():
                try:
                    self._lookup_player_pos(player)
                except Exception:
                    pass
                try:
                    self._walk_to_player(player)
                except Exception:
                    pass
                try:
                    pre = self._dim_prefix()
                    rcon_cmd(f'{pre}execute as @e[tag={self._entity_tag}] at @s run particle minecraft:witch ~ ~1 ~ 0.3 0.5 0.3 0.02 15')
                except Exception:
                    pass
            walk_thread = threading.Thread(target=_walk_and_prep, daemon=True)
            walk_thread.start()
        else:
            # Just lookup pos without walking
            try:
                self._lookup_player_pos(player)
            except Exception:
                pass

        # Get AI response (runs in parallel with walking)
        raw_response = self._ask_mlx(player, prompt)

        # Wait for walk to finish before executing actions
        if walk_thread:
            walk_thread.join(timeout=5)

        # Execute any action tags (only on triggered or if mode allows autonomous actions)
        if is_triggered or self.mode in ("evil", "loki"):
            self._execute_actions(raw_response, default_player=player)

        # Strip action tags for chat display
        clean_response = self._strip_actions(raw_response)
        if not clean_response or clean_response == "...":
            clean_response = "The spirits stir..."

        self._chat_log.append({
            "type": "npc",
            "player": player,
            "text": clean_response,
            "time": time.strftime("%H:%M:%S"),
        })

        # Triggered messages go to that player, auto-chat goes to everyone
        target = player if is_triggered else "@a"
        self._say(target, clean_response)

        # Remember this player — overwrite with latest impression
        self._remember_player(player, f"Said: \"{message[:50]}\" — I replied: \"{clean_response[:50]}\"")


    # Common words for gibberish detection — must be generous to avoid false positives
    _COMMON_WORDS = set(
        # Core English
        "a an the and or but in on at to of is it i me my we us he she "
        "they you your his her its our their this that these those with for "
        "from by was were been be am are do did does not no yes all any "
        "some many much more most can could will would shall should may might "
        "have has had let get got go went gone come came say said see saw "
        "know knew make made take took give gave find found think thought "
        "tell told keep kept help here there where when how what who why "
        "if so up out about into over after before just also very too quite "
        "well back now then than still even only each every both few new old "
        "good bad great big small long short first last next other another "
        "time day way thing world life man people part place hand right left "
        "eye face door look like want need use try ask work play run move "
        "turn open close start stop end hold live die kill speak hear feel "
        "wait watch show meet follow leave call read write set put stand "
        "bring cut fall send build grow learn change through between such "
        "while because until again once never always oh hey hello hi okay "
        "sure down away off own its being "
        # Fantasy / RPG
        "shall bestow grant upon thee thy thou hast doth verily behold alas "
        "mortal traveler seeker keeper oracle wisdom knowledge greetings "
        "ancient secret hidden power magic quest adventure brave warrior "
        "fool foolish puny pathetic wretched suffer darkness light shadow "
        "curse bless heal protect destroy conjure summon banish vanish "
        "mighty powerful fearless generous cruel wicked trickster chaos "
        "storm thunder lightning flame blaze fury wrath doom fate destiny "
        "spirit soul realm kingdom treasure fortune glory honor "
        "enchanted mystical arcane divine sacred dark evil holy pure "
        "wizard sorcerer sage mage witch warlock priest knight hero "
        "dragon wither phantom creeper zombie skeleton spider blaze ghast "
        "enderman guardian ravager pillager vindicator warden "
        # Minecraft-specific
        "game player mine craft block stone wood iron gold diamond sword "
        "bow armor shield potion enchant spawn nether end portal "
        "netherite elytra trident totem shulker emerald obsidian bedrock "
        "helmet chestplate leggings boots pickaxe axe shovel hoe "
        "ingot nugget ore coal redstone lapis quartz copper amethyst "
        "biome desert jungle swamp ocean mountain plains forest taiga "
        "cave treasure chest lava water fire cobblestone dirt sand gravel "
        "apple bread beef steak pork chicken fish cake pie cookie "
        "regeneration strength speed haste resistance absorption saturation "
        "invisibility blindness slowness weakness poison wither hunger "
        "brewing crafting smelting mining building farming fishing trading "
        "villager golem cat dog wolf horse pig cow sheep chicken rabbit fox "
        "dimension overworld ender pearl rod blaze rod eye "
        # Common adjectives/adverbs
        "really truly actually just already enough quite perhaps maybe "
        "probably certainly definitely absolutely exactly simply merely "
        "beautiful wonderful terrible horrible amazing incredible "
        "precious rare valuable dangerous safe warm cold hot deep high "
        "far near above below inside outside together alone "
        "happy sad angry afraid hungry tired sick hurt lost free "
        "ready able willing careful quick slow loud quiet "
        # Trickster / Loki vocabulary
        "trick prank joke fool surprise oops whoops haha mischief "
        "lie liar deceive cheat steal swap teleport random chaos "
        "boring fun funny hilarious amusing entertaining delightful "
        "trust betray promise generous sorry enjoy catch bye farewell "
        "gift present reward punishment deserve worthy unworthy".split()
    )

    _ACTION_TAG_RE = re.compile(r'\[[A-Z_]+\s[^\]]*\]')

    @staticmethod
    def _sanitize_response(text, preserve_actions=False):
        """Strip garbage from LLM output with aggressive gibberish detection.
        If preserve_actions=True, action tags like [GIVE player item 5] are kept intact.
        """
        saved_tags = []
        working = text
        if preserve_actions:
            for i, m in enumerate(NpcChat._ACTION_TAG_RE.finditer(working)):
                placeholder = f"__ACT{i}__"
                saved_tags.append((placeholder, m.group()))
            for placeholder, tag in saved_tags:
                working = working.replace(tag, placeholder, 1)

        cleaned = re.sub(r'[^\x20-\x7E]', '', working)
        cleaned = re.sub(r'[{}<>|\\]', '', cleaned)
        cleaned = re.sub(r'([.!?,;:])\1{2,}', r'\1', cleaned)
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        degen = re.search(r'[^a-zA-Z0-9\s.,!?\'\-_]{3,}', cleaned)
        if degen:
            cleaned = cleaned[:degen.start()].rstrip()

        words = cleaned.split()
        cut_idx = None
        consec_unknown = 0
        word_positions = []
        pos = 0
        for w in words:
            if w.startswith("__ACT"):
                consec_unknown = 0
                start = cleaned.find(w, pos)
                word_positions.append(start)
                pos = start + len(w)
                continue
            start = cleaned.find(w, pos)
            word_positions.append(start)
            pos = start + len(w)
            low = re.sub(r'[^a-z]', '', w.lower())
            if len(low) > 2 and low not in NpcChat._COMMON_WORDS:
                consec_unknown += 1
            else:
                consec_unknown = 0
            if consec_unknown >= 8:
                cut_idx = word_positions[len(word_positions) - 8]
                break
        if cut_idx is not None and cut_idx > 20:
            cleaned = cleaned[:cut_idx].rstrip(' ,;:-')
        elif cut_idx is not None:
            cleaned = ""

        sentences = re.split(r'(?<=[.!?])\s+', cleaned.strip())
        if len(sentences) > 5:
            cleaned = ' '.join(sentences[:5])
        if len(cleaned) > 500:
            cleaned = cleaned[:500].rsplit(' ', 1)[0] + '...'

        if preserve_actions:
            for placeholder, tag in saved_tags:
                cleaned = cleaned.replace(placeholder, tag)

        return cleaned.strip() or "..."

    _VARIETY_SEEDS = [
        "Respond creatively.", "Use different words than before.",
        "Surprise me with something new.", "Be original this time.",
        "Try a completely different approach.", "Say something unexpected.",
        "Pick a different action than last time.", "Change your style.",
        "Be more dramatic.", "Be more subtle.", "Be mysterious.",
        "Be theatrical.", "Be sarcastic.", "Be poetic.", "Be blunt.",
    ]

    def _ask_mlx(self, player, prompt):
        """Send prompt to MLX server and return the response text with action tags preserved."""
        import random
        if player not in self._history:
            self._history[player] = deque(maxlen=self._history_len)

        # Build anti-repetition context
        seed = random.choice(self._VARIETY_SEEDS)
        anti_repeat = ""
        if self._recent_responses:
            last_few = list(self._recent_responses)[-3:]
            avoid_list = "; ".join(f'"{r[:50]}"' for r in last_few)
            anti_repeat = f"\nDo NOT say anything similar to these recent replies: {avoid_list}\n"

        # Inject player name so the LLM can use it in action tags
        is_npc_conversation = player.startswith("__npc_")
        real_player = player if (not player.startswith("__")) else "someone"
        player_context = (
            f"\nThe player talking to you is named: {real_player}\n"
            f"IMPORTANT: When using action tags, use EXACTLY this name: {real_player}\n"
            f"Example: [GIVE {real_player} diamond 1] or [LIGHTNING {real_player}]\n"
        )

        # Inject awareness of other active NPCs
        npc_awareness = ""
        other_npcs = self._get_other_npcs()
        if other_npcs:
            npc_list = []
            for onpc in other_npcs:
                npc_list.append(f"  - {onpc.npc_name} ({onpc.mode} god)")
            npc_lines = "\n".join(npc_list)
            npc_awareness = (
                f"\n=== OTHER GODS IN THIS WORLD ===\n"
                f"You are NOT alone. These other deities are also active:\n"
                f"{npc_lines}\n"
                f"You can interact with them! Taunt, challenge, ally, fight, or scheme against them.\n"
                f"If you encounter one, react to their presence with your personality.\n"
                f"Good gods may defend players from evil ones. Evil gods may attack other gods.\n"
                f"Loki plays everyone against each other.\n"
            )
            if is_npc_conversation:
                npc_awareness += (
                    "You are currently TALKING TO ANOTHER GOD, not a player. "
                    "Be dramatic, theatrical, and assertive. Show your divine power!\n"
                )

        # Dynamically tell the LLM which ability categories are enabled/disabled
        ability_notes = self._build_ability_context()

        # Inject persistent memory so the god remembers past experiences
        memory_context = ""
        mem_summary = self._get_memory_summary(1000)
        if mem_summary and len(mem_summary) > 50:
            memory_context = (
                "\n=== YOUR MEMORY (things you've experienced, built, and learned) ===\n"
                + mem_summary
                + "\nUse your memories to inform your responses. Reference past events, "
                "players you remember, things you've built, and opinions about other gods.\n"
            )

        system_msg = self.personality + player_context + npc_awareness + ability_notes + memory_context + anti_repeat + f"\n({seed})"

        messages = [{"role": "system", "content": system_msg}]
        messages.extend(list(self._history[player]))
        messages.append({"role": "user", "content": prompt})

        _MLX_TIMEOUT = 90  # model swapping can take a while on MLX

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "repetition_penalty": self.repetition_penalty,
            }

            text = None
            last_err = None
            for _attempt in range(3):
                try:
                    with _mlx_lock:
                        resp = requests.post(
                            f"http://{MLX_HOST}/v1/chat/completions",
                            json=payload,
                            timeout=_MLX_TIMEOUT,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    text = self._sanitize_response(raw, preserve_actions=True)
                    break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    last_err = e
                    import logging
                    logging.warning(f"[{self.npc_name}] MLX attempt {_attempt+1} failed ({type(e).__name__}): {e}")
                    time.sleep(2 * (_attempt + 1))
                except Exception as e:
                    last_err = e
                    import logging
                    logging.warning(f"[{self.npc_name}] MLX attempt {_attempt+1} error: {type(e).__name__}: {e}")
                    break  # non-transient error, don't retry

            if text is None:
                raise last_err or Exception("No response from MLX after retries")

            # Deduplication: if this response is too similar to a recent one, retry once
            clean_check = self._strip_actions(text).lower().strip()
            for prev in self._recent_responses:
                if clean_check and prev.lower().strip() == clean_check:
                    payload["temperature"] = min(self.temperature + 0.3, 1.0)
                    try:
                        with _mlx_lock:
                            resp2 = requests.post(
                                f"http://{MLX_HOST}/v1/chat/completions",
                                json=payload, timeout=_MLX_TIMEOUT,
                            )
                            resp2.raise_for_status()
                            raw2 = resp2.json()["choices"][0]["message"]["content"].strip()
                        text = self._sanitize_response(raw2, preserve_actions=True)
                    except Exception:
                        pass  # keep original text if dedup retry fails
                    break

            self._recent_responses.append(self._strip_actions(text))
        except requests.exceptions.ConnectionError:
            text = f"{self.npc_name} is sleeping... the MLX server is unreachable."
        except Exception as e:
            import logging
            logging.error(f"[{self.npc_name}] LLM call failed: {type(e).__name__}: {e}")
            text = f"{self.npc_name} stumbles... something went wrong ({type(e).__name__})."

        self._history[player].append({"role": "user", "content": prompt})
        clean_for_history = self._strip_actions(text)
        self._history[player].append({"role": "assistant", "content": clean_for_history})

        return text

    @staticmethod
    def _escape_json(text):
        """Escape text for safe embedding inside JSON strings in RCON commands."""
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    def _say(self, target, text):
        """Send NPC response to players via RCON tellraw (thread-safe)."""
        safe = self._escape_json(text)
        chunks = [safe[i:i+400] for i in range(0, len(safe), 400)]
        for chunk in chunks:
            cmd = (
                f'tellraw {target} ['
                f'{{"text":"[{self.npc_name}] ","color":"{self.color}","bold":true}},'
                f'{{"text":"{chunk}","color":"white","italic":true}}'
                f']'
            )
            result = rcon_cmd(cmd)
            if "[RCON ERROR]" in result:
                self._chat_log.append({
                    "type": "error",
                    "text": f"RCON send failed: {result}",
                    "time": time.strftime("%H:%M:%S"),
                })
                return
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# NPC Manager — supports multiple NPCs simultaneously
# ---------------------------------------------------------------------------
_npcs = {}  # npc_id -> NpcChat instance
_npcs_lock = threading.Lock()


def _get_npc(npc_id=None):
    """Get NPC by id. Returns first running NPC or 'good' default."""
    if npc_id and npc_id in _npcs:
        return _npcs[npc_id]
    # Fallback: return first running or create good default
    for npc in _npcs.values():
        if npc.running:
            return npc
    if "good" not in _npcs:
        _npcs["good"] = NpcChat(npc_id="good", mode="good")
    return _npcs["good"]


def _get_or_create_npc(mode):
    """Get or create NPC for a specific mode."""
    npc_id = mode
    if npc_id not in _npcs:
        _npcs[npc_id] = NpcChat(npc_id=npc_id, mode=mode)
    return _npcs[npc_id]


# Backward compat: create a default good NPC
_npcs["good"] = NpcChat(npc_id="good", mode="good")


# ---------------------------------------------------------------------------
# API — NPC Management (multi-NPC aware)
# ---------------------------------------------------------------------------
@app.route("/api/npc/list")
@login_required
def api_npc_list():
    """List all NPC instances and their status."""
    result = []
    for npc_id, npc in _npcs.items():
        result.append({
            "npc_id": npc_id,
            "mode": npc.mode,
            "name": npc.npc_name,
            "running": npc.running,
            "trigger": npc.trigger,
        })
    return jsonify({"npcs": result})


@app.route("/api/npc/spawn", methods=["POST"])
@login_required
def api_npc_spawn():
    """Spawn a new NPC with a given mode. Returns the NPC info."""
    data = request.json or {}
    mode = data.get("mode", "good")
    if mode not in NpcChat.MODES:
        return jsonify({"error": f"Invalid mode: {mode}"}), 400
    npc = _get_or_create_npc(mode)
    if not npc.running:
        npc.start()
    return jsonify({"status": "spawned", "npc_id": npc.npc_id, "name": npc.npc_name,
                     "mode": npc.mode, "trigger": npc.trigger})


@app.route("/api/npc/despawn", methods=["POST"])
@login_required
def api_npc_despawn():
    """Stop and remove an NPC by id or mode."""
    data = request.json or {}
    npc_id = data.get("npc_id") or data.get("mode", "good")
    if npc_id in _npcs:
        _npcs[npc_id].stop()
        return jsonify({"status": "despawned", "npc_id": npc_id})
    return jsonify({"error": f"NPC '{npc_id}' not found"}), 404


@app.route("/api/npc/clash", methods=["POST"])
@login_required
def api_npc_clash():
    """Trigger a battle between two NPCs."""
    data = request.json or {}
    npc1_id = data.get("npc1") or data.get("attacker")
    npc2_id = data.get("npc2") or data.get("defender")
    if not npc1_id or not npc2_id:
        return jsonify({"error": "Provide npc1 and npc2 ids"}), 400
    if npc1_id not in _npcs or npc2_id not in _npcs:
        return jsonify({"error": "One or both NPCs not found"}), 404
    npc1 = _npcs[npc1_id]
    npc2 = _npcs[npc2_id]
    if not npc1.running or not npc2.running:
        return jsonify({"error": "Both NPCs must be running"}), 400
    threading.Thread(
        target=npc1._npc_interact, args=(npc2,), daemon=True
    ).start()
    return jsonify({
        "status": "clash_initiated",
        "attacker": {"npc_id": npc1.npc_id, "name": npc1.npc_name},
        "defender": {"npc_id": npc2.npc_id, "name": npc2.npc_name},
    })


@app.route("/api/npc/build", methods=["POST"])
@login_required
def api_npc_build():
    """Trigger a structure build for an NPC."""
    import random as _rng
    data = request.json or {}
    npc_id = data.get("npc_id") or data.get("mode")
    build_type = data.get("build_type", "random")  # random, surface, subterranean

    if npc_id and npc_id in _npcs:
        npc = _npcs[npc_id]
    else:
        return jsonify({"error": "NPC not found or not running"}), 404

    if not npc.running:
        return jsonify({"error": "NPC must be running to build"}), 400

    # AI-powered creative building
    threading.Thread(target=npc._ai_solo_build, daemon=True).start()
    return jsonify({
        "status": "building_ai",
        "npc_id": npc.npc_id,
        "message": f"{npc.npc_name} is creatively designing a new structure...",
    })


@app.route("/api/npc/collab_build", methods=["POST"])
@login_required
def api_npc_collab_build():
    """Trigger a collaborative build between two NPCs."""
    import random as _rng
    running_npcs = [n for n in _npcs.values() if n.running]
    if len(running_npcs) < 2:
        return jsonify({"error": "Need at least 2 running NPCs for collab build"}), 400

    npc1, npc2 = _rng.sample(running_npcs, 2)
    threading.Thread(target=npc1._collab_build, args=(npc2,), daemon=True).start()
    return jsonify({
        "status": "collab_building",
        "builders": [
            {"npc_id": npc1.npc_id, "name": npc1.npc_name},
            {"npc_id": npc2.npc_id, "name": npc2.npc_name},
        ],
    })


@app.route("/api/npc/memory")
@login_required
def api_npc_memory():
    """Get a god's memory file contents."""
    npc_id = request.args.get("npc_id") or request.args.get("mode", "good")
    if npc_id in _npcs:
        npc = _npcs[npc_id]
        return jsonify({"npc_id": npc_id, "name": npc.npc_name, "memory": npc._memory_cache or ""})
    # Try to read the file directly even if NPC isn't running
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "god_memories")
    path = os.path.join(memory_dir, f"{npc_id}_self.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            return jsonify({"npc_id": npc_id, "memory": f.read()})
    return jsonify({"npc_id": npc_id, "memory": "(no memory file yet)"})


@app.route("/api/npc/memory/clear", methods=["POST"])
@login_required
def api_npc_memory_clear():
    """Clear a god's memory and start fresh."""
    data = request.json or {}
    npc_id = data.get("npc_id") or data.get("mode", "good")
    if npc_id in _npcs:
        npc = _npcs[npc_id]
        fresh = npc._create_memory_template()
        npc._save_memory_raw(fresh)
        return jsonify({"status": "cleared", "npc_id": npc_id})
    # Clear file directly
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "god_memories")
    path = os.path.join(memory_dir, f"{npc_id}_self.md")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"status": "cleared", "npc_id": npc_id})


@app.route("/api/npc/memory/all")
@login_required
def api_npc_memory_all():
    """Get all gods' memories."""
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "god_memories")
    memories = {}
    if os.path.isdir(memory_dir):
        for fname in os.listdir(memory_dir):
            if fname.endswith("_self.md"):
                npc_id = fname.replace("_self.md", "")
                with open(os.path.join(memory_dir, fname), "r") as f:
                    memories[npc_id] = f.read()
    return jsonify({"memories": memories})


@app.route("/api/npc/chat_all")
@login_required
def api_npc_chat_all():
    """Get merged chat logs from ALL running NPCs, sorted by time."""
    merged = []
    mode_colors = {"good": "#00e676", "evil": "#ff2d55", "loki": "#ffcc00"}
    for npc in _npcs.values():
        name = npc.npc_name
        mode = npc.mode
        color = mode_colors.get(mode, "#b44dff")
        for entry in list(npc._chat_log):
            e = dict(entry)
            e["god"] = name
            e["god_mode"] = mode
            e["god_color"] = color
            merged.append(e)
    # Sort by time string (HH:MM:SS) — works for same-day
    merged.sort(key=lambda x: x.get("time", ""))
    # Return last 150 entries
    return jsonify({"chat_log": merged[-150:]})


@app.route("/api/npc/status")
@login_required
def api_npc_status():
    """Get NPC status. Pass ?npc_id=X or returns first/default."""
    npc_id = request.args.get("npc_id")
    npc = _get_npc(npc_id)
    status = npc.get_status()
    status["npc_id"] = npc.npc_id
    # Also include info about all NPCs
    status["all_npcs"] = [
        {"npc_id": n.npc_id, "mode": n.mode, "name": n.npc_name,
         "running": n.running, "trigger": n.trigger}
        for n in _npcs.values()
    ]
    return jsonify(status)


@app.route("/api/npc/start", methods=["POST"])
@login_required
def api_npc_start():
    """Start an NPC. Pass npc_id or mode in body."""
    data = request.json or {}
    mode = data.get("mode") or data.get("npc_id", "good")
    npc = _get_or_create_npc(mode)
    npc.start()
    return jsonify({"status": "running", "npc_id": npc.npc_id, "name": npc.npc_name})


@app.route("/api/npc/stop", methods=["POST"])
@login_required
def api_npc_stop():
    """Stop an NPC."""
    data = request.json or {}
    npc_id = data.get("npc_id") or data.get("mode")
    if npc_id and npc_id in _npcs:
        _npcs[npc_id].stop()
        return jsonify({"status": "stopped", "npc_id": npc_id})
    # Fallback: stop all
    for npc in _npcs.values():
        npc.stop()
    return jsonify({"status": "all_stopped"})


@app.route("/api/mlx/models")
@login_required
def api_mlx_models():
    """Return list of available MLX models from the MLX server."""
    default_model = "mlx-community/Qwen2.5-7B-Instruct-4bit"
    try:
        r = requests.get(f"http://{MLX_HOST}/v1/models", timeout=3)
        r.raise_for_status()
        data = r.json()
        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            # Extract a short display name from the full model ID
            short = mid.split("/")[-1] if "/" in mid else mid
            # Estimate size category from name
            size = "?"
            for s in ["1B", "3B", "7B", "8B", "13B", "14B", "32B", "70B"]:
                if s in short:
                    size = s
                    break
            models.append({
                "id": mid,
                "short_name": short,
                "size": size,
                "is_default": mid == default_model,
            })
        # Sort by size (smaller first)
        size_order = {"1B": 1, "3B": 2, "7B": 3, "8B": 4, "13B": 5, "14B": 6, "32B": 7, "70B": 8, "?": 9}
        models.sort(key=lambda m: size_order.get(m["size"], 99))
        return jsonify({"models": models, "default": default_model})
    except Exception as e:
        return jsonify({"models": [{"id": default_model, "short_name": "Qwen2.5-7B-Instruct-4bit", "size": "7B", "is_default": True}], "default": default_model, "error": str(e)})


@app.route("/api/npc/config", methods=["POST"])
@login_required
def api_npc_config():
    """Update NPC configuration."""
    data = request.json or {}
    npc_id = data.pop("npc_id", None)
    npc = _get_npc(npc_id)
    npc.update_config(data)
    return jsonify({"status": "ok", "config": npc.get_status()})


@app.route("/api/npc/test", methods=["POST"])
@login_required
def api_npc_test():
    """Test the NPC with a prompt."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    prompt = data.get("prompt", "Hello")
    player = data.get("player", "TestPlayer")
    response = npc.test_prompt(prompt, player)
    return jsonify({"prompt": prompt, "response": response})


@app.route("/api/npc/say", methods=["POST"])
@login_required
def api_npc_say():
    """Send a message as an NPC to players."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    message = data.get("message", "").strip()
    target = data.get("target", "@a").strip() or "@a"
    if not message:
        return jsonify({"error": "No message provided"}), 400

    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    cmd_str = (
        f'tellraw {target} ['
        f'{{"text":"[{npc.npc_name}] ","color":"{npc.color}","bold":true}},'
        f'{{"text":"{safe}","color":"white","italic":true}}'
        f']'
    )
    result = rcon_cmd(cmd_str)

    npc._chat_log.append({
        "type": "npc",
        "player": target,
        "text": message,
        "time": time.strftime("%H:%M:%S"),
    })

    return jsonify({"status": "sent", "target": target, "message": message, "rcon": result})


@app.route("/api/npc/ask_and_say", methods=["POST"])
@login_required
def api_npc_ask_and_say():
    """Generate AI response and say it in-game."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    prompt = data.get("prompt", "").strip()
    target = data.get("target", "@a").strip() or "@a"
    player_context = data.get("player", "Admin")
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    response = npc._ask_mlx(player_context, prompt)
    npc._say(target, response)

    npc._chat_log.append({
        "type": "npc",
        "player": target,
        "text": response,
        "time": time.strftime("%H:%M:%S"),
    })

    return jsonify({"status": "sent", "prompt": prompt, "response": response, "target": target})


@app.route("/api/npc/move", methods=["POST"])
@login_required
def api_npc_move():
    """Move an NPC entity."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    player_name = data.get("player")

    if player_name:
        pos_resp = rcon_cmd(f"data get entity {player_name} Pos")
        dim_resp = rcon_cmd(f"data get entity {player_name} Dimension")
        pos_match = re.search(r'\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]', pos_resp or "")
        dim_match = re.search(r'"minecraft:(\w+)"', dim_resp or "")
        if not pos_match:
            return jsonify({"error": f"Could not find player '{player_name}'"}), 404
        x = float(pos_match.group(1)) + 3
        z = float(pos_match.group(3)) + 3
        dim = dim_match.group(1) if dim_match else "overworld"
    else:
        x = float(data.get("x", npc.home_x))
        z = float(data.get("z", npc.home_z))
        dim = data.get("dimension", npc.home_dimension)

    npc.move_entity(x, z, dim)
    return jsonify({"status": "moved", "x": x, "z": z, "dimension": dim})


@app.route("/api/npc/respawn", methods=["POST"])
@login_required
def api_npc_respawn():
    """Respawn an NPC entity."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    if npc.running:
        npc._spawn_entity()
        return jsonify({"status": "respawned"})
    return jsonify({"error": "NPC is not running"}), 400


@app.route("/api/npc/history/clear", methods=["POST"])
@login_required
def api_npc_clear_history():
    """Clear NPC conversation history."""
    data = request.json or {}
    npc_id = data.get("npc_id")
    npc = _get_npc(npc_id)
    npc._history.clear()
    npc._chat_log.clear()
    return jsonify({"status": "cleared"})


@app.route("/api/map/markers")
@login_required
def api_map_markers():
    """Return NPC + active bot markers for the map overlay."""
    markers = []

    # Map marker colors — high-contrast against terrain (separate from chat colors)
    _mode_map_colors = {"good": "#b44dff", "evil": "#ff2d55", "loki": "#ffcc00"}

    # Only include running NPC markers (no greyed-out placeholders)
    for npc in _npcs.values():
        if not npc.running:
            continue
        live_x, live_y, live_z = npc.home_x, 64, npc.home_z
        live_dim = npc.home_dimension
        try:
            pos = npc._get_entity_pos()
            if pos:
                live_x, live_y, live_z = pos[0], pos[1], pos[2]
        except Exception:
            pass
        markers.append({
            "type": "npc",
            "npc_id": npc.npc_id,
            "name": npc.npc_name,
            "mode": npc.mode,
            "status": "running",
            "x": live_x,
            "y": live_y,
            "z": live_z,
            "home_x": npc.home_x,
            "home_z": npc.home_z,
            "home_dimension": live_dim,
            "marker_color": _mode_map_colors.get(npc.mode, "#b44dff"),
            "last_interact_player": npc.last_interact_player,
            "last_interact_x": npc.last_interact_x,
            "last_interact_z": npc.last_interact_z,
            "last_interact_dim": npc.last_interact_dim,
            "last_interact_time": npc.last_interact_time,
            "color": npc.color,
        })

    # Active bots
    for bid, bot in _bots.items():
        if bot["status"] == "running":
            markers.append({
                "type": "bot",
                "id": bid,
                "name": bot["name"],
                "status": bot["status"],
                "mode": bot.get("mode", "all"),
                "runs_done": bot.get("runs_done", 0),
                "interval": bot["interval"],
            })

    return jsonify({"markers": markers})


def _hide_command_feedback():
    """Suppress RCON command output from player chat on startup."""
    import threading
    def _apply():
        time.sleep(5)  # Wait for MC server to be ready
        try:
            rcon_cmd("gamerule send_command_feedback false")
            rcon_cmd("gamerule command_block_output false")
            rcon_cmd("gamerule log_admin_commands false")
        except Exception:
            pass
    threading.Thread(target=_apply, daemon=True).start()

_hide_command_feedback()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")

# Coach Dad's Agnostic Minecraft Server

One server, every platform. Java + Bedrock players all in the same world.

## What's Inside

- **PaperMC** — high-performance Minecraft server
- **GeyserMC** — lets Bedrock players (phone/console) join a Java server
- **Floodgate** — Bedrock players don't need a Java account
- **playit.gg** — free tunnel to the internet (no port forwarding needed)
- **Admin Panel** — web-based god-mode control panel at `localhost:8420`

## Quick Start

### 1. Install Docker

Install [OrbStack](https://orbstack.dev) (recommended for Mac) or Docker Desktop.

### 2. Set Up playit.gg (free)

1. Go to [playit.gg](https://playit.gg) and create a free account
2. Create a new **Agent** and copy the secret key
3. Edit `.env` in this folder and paste your key:
   ```
   PLAYIT_SECRET=your-secret-key-here
   ```
4. In the playit.gg dashboard, create two tunnels pointing to your agent:
   - **Minecraft Java** → TCP, port 25565
   - **Minecraft Bedrock** → UDP, port 19132

### 3. Launch

```bash
docker compose up -d
```

First boot takes ~2 minutes (downloads server + plugins). After that, ~15 seconds.

### 4. Connect

| Platform | How to Connect |
|----------|---------------|
| **Java Edition** (Mac/PC/Linux) | Direct Connect → `your-playit-address` |
| **Bedrock** (iOS/Android/Xbox/PS/Switch) | Add Server → `your-playit-address`, port from playit dashboard |
| **LAN** (same network) | `localhost:25565` (Java) or `localhost:19132` (Bedrock) |

### 5. Admin Panel

Open **http://localhost:8420** in your browser.

Default login:
- Username: `coach_dad`
- Password: `changeme`

(Change these in `.env` before going live!)

## Admin Panel Features

| Tab | What you can do |
|-----|----------------|
| **Dashboard** | Server status, online players, quick actions |
| **Players** | Gamemode, teleport, heal, kill, OP, ban, effects, god mode |
| **Cheats & Items** | Give diamond/netherite sets, god weapons, any item by ID |
| **Game Settings** | Difficulty, time, weather, 16+ game rule toggles, world border |
| **Troll Scripts** | Lightning storms, mob armies, TNT rain, bedrock cages, fake shutdowns, and more |
| **World** | Kill mobs, broadcast messages, set spawn, save world |
| **Console** | Raw RCON command line — run anything |

## Commands

| Command | What it does |
|---------|-------------|
| `docker compose up -d` | Start server + tunnel + admin panel |
| `docker compose down` | Stop everything |
| `docker compose logs -f mc` | Watch server logs |
| `docker compose logs -f tunnel` | Watch tunnel logs |
| `docker compose logs -f admin` | Watch admin panel logs |
| `docker compose exec mc rcon-cli` | Server console (run commands) |
| `docker compose restart mc` | Restart server only |
| `./mc.sh start` | Same as docker compose up (with banner) |

## Configuration

Edit `.env` to change settings. After editing, restart:

```bash
docker compose down && docker compose up -d
```

## World Data

Your world is saved in `./data/`. Back it up by copying that folder.

```bash
# Quick backup (or use ./mc.sh backup)
cp -r data "backups/$(date +%Y-%m-%d)"
```

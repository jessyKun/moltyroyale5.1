import json
import asyncio
import websockets

from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS
from bot.strategy.brain import decide_action, reset_game_state, learn_from_map
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    from bot.strategy.brain import _map_knowledge

    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            region_id = region.get("id", "")
            if region_id:
                _map_knowledge["death_zones"].add(region_id)

    for connected in view.get("connectedRegions", []):
        if isinstance(connected, dict) and connected.get("isDeathZone"):
            region_id = connected.get("id", "")
            if region_id:
                _map_knowledge["death_zones"].add(region_id)

    current_region = view.get("currentRegion", {})
    if isinstance(current_region, dict) and current_region.get("isDeathZone"):
        region_id = current_region.get("id", "")
        if region_id:
            _map_knowledge["death_zones"].add(region_id)

    for death_zone in view.get("pendingDeathzones", []):
        if isinstance(death_zone, dict):
            region_id = death_zone.get("id", "")
            if region_id:
                _map_knowledge["death_zones"].add(region_id)
        elif isinstance(death_zone, str):
            _map_knowledge["death_zones"].add(death_zone)


class WebSocketEngine:
    def __init__(self, game_id: str, agent_id: str):
        self.game_id = game_id
        self.agent_id = agent_id
        self.action_sender = ActionSender()
        self.ws = None
        self.game_result = None
        self.last_view = None
        self._ping_task = None
        self._running = False
        self._map_just_used = False
        self.dashboard_key = agent_id
        self.dashboard_name = "Agent"

    async def _connect(self, headers: dict):
        try:
            return websockets.connect(
                WS_URL,
                additional_headers=headers,
                ping_interval=None,
                max_size=2**20,
            )
        except TypeError:
            return websockets.connect(
                WS_URL,
                extra_headers=headers,
                ping_interval=None,
                max_size=2**20,
            )

    async def run(self) -> dict:
        api_key = get_api_key()

        headers = {
            "X-API-Key": api_key,
            "X-Version": SKILL_VERSION,
        }

        self._running = True
        retry_count = 0
        max_retries = 5

        while self._running and retry_count < max_retries:
            try:
                log.info("Connecting WebSocket to %s...", WS_URL)

                try:
                    connection = websockets.connect(
                        WS_URL,
                        additional_headers=headers,
                        ping_interval=None,
                        max_size=2**20,
                    )
                except TypeError:
                    connection = websockets.connect(
                        WS_URL,
                        extra_headers=headers,
                        ping_interval=None,
                        max_size=2**20,
                    )

                async with connection as ws:
                    self.ws = ws
                    retry_count = 0

                    log.info("WebSocket connected for game=%s", self.game_id)

                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw_message in ws:
                        try:
                            message = json.loads(raw_message)

                            if not isinstance(message, dict):
                                log.warning("Non-dict WS message: %s", type(message).__name__)
                                continue

                            message_type = message.get("type", "unknown")
                            log.debug("WS recv: type=%s", message_type)

                            result = await self._handle_message(message)

                            if result is not None:
                                self._running = False
                                return result

                        except json.JSONDecodeError:
                            log.warning("Non-JSON message: %s", str(raw_message)[:100])

            except websockets.exceptions.ConnectionClosed as exc:
                retry_count += 1
                log.warning(
                    "WebSocket closed: code=%s reason=%s retry=%d/%d",
                    exc.code,
                    exc.reason,
                    retry_count,
                    max_retries,
                )

                if self._ping_task:
                    self._ping_task.cancel()

                await asyncio.sleep(min(2**retry_count, 30))

            except Exception as exc:
                retry_count += 1
                log.error("WebSocket error: %s retry=%d/%d", exc, retry_count, max_retries)

                if self._ping_task:
                    self._ping_task.cancel()

                await asyncio.sleep(min(2**retry_count, 30))

        return self.game_result or {"status": "disconnected"}

    async def _handle_message(self, message: dict) -> dict | None:
        message_type = message.get("type", "")

        if message_type == "agent_view":
            view = message.get("view") or message.get("data") or {}

            if isinstance(view, dict) and view:
                self.last_view = view
                reason = message.get("reason", "initial")
                self_data = view.get("self", {})
                alive = self_data.get("isAlive", "?")
                hp = self_data.get("hp", "?")
                ep = self_data.get("ep", "?")

                log.info(
                    "agent_view reason=%s alive=%s HP=%s EP=%s",
                    reason,
                    alive,
                    hp,
                    ep,
                )

                await self._on_agent_view(view)

            else:
                log.warning("agent_view with empty or invalid view: %s", str(view)[:100])

        elif message_type == "action_result":
            success = message.get("success", False)

            self.action_sender.can_act = message.get(
                "canAct",
                self.action_sender.can_act,
            )
            self.action_sender.cooldown_remaining_ms = message.get(
                "cooldownRemainingMs",
                0,
            )

            if success:
                data = message.get("data", {})
                action_message = data.get("message", "") if isinstance(data, dict) else str(data)

                log.info(
                    "Action OK: %s canAct=%s",
                    action_message,
                    message.get("canAct"),
                )

                if "map" in str(action_message).lower():
                    self._map_just_used = True

            else:
                error = message.get("error", {})
                error_code = error.get("code", "") if isinstance(error, dict) else str(error)
                error_message = error.get("message", "") if isinstance(error, dict) else ""

                log.warning(
                    "Action FAILED: %s - %s canAct=%s",
                    error_code,
                    error_message,
                    message.get("canAct"),
                )

        elif message_type == "can_act_changed":
            self.action_sender.can_act = message.get("canAct", True)
            self.action_sender.cooldown_remaining_ms = message.get(
                "cooldownRemainingMs",
                0,
            )

            log.info("can_act_changed: canAct=%s", message.get("canAct"))

            if self.last_view and message.get("canAct"):
                await self._on_agent_view(self.last_view)

        elif message_type == "turn_advanced":
            turn_number = message.get("turn", "?")
            view = message.get("view")

            if not view and isinstance(message.get("data"), dict):
                view = message["data"].get("view")
                turn_number = message["data"].get("turn", turn_number)

            log.info("Turn %s - processing view", turn_number)

            if view and isinstance(view, dict):
                self.last_view = view
                await self._on_agent_view(view)
            elif self.last_view:
                await self._on_agent_view(self.last_view)
            else:
                log.warning("Turn advanced but no view data available")

        elif message_type == "game_ended":
            log.info("GAME ENDED")
            reset_game_state()
            self.game_result = message
            return message

        elif message_type == "event":
            data = message.get("data", {})
            event_type = message.get("eventType", "")

            if not event_type and isinstance(data, dict):
                event_type = data.get("eventType", "")

            log.debug("Event: %s", event_type)

        elif message_type == "waiting":
            log.info("Game is waiting for players")

        elif message_type == "pong":
            pass

        elif message_type == "error":
            data = message.get("data", {})
            error_message = message.get("message")

            if not error_message and isinstance(data, dict):
                error_message = data.get("message")

            log.error("Server error: %s", error_message or str(message))

        else:
            log.info("Unknown WS message type=%s keys=%s", message_type, list(message.keys()))

        return None

    async def _on_agent_view(self, view: dict):
        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})

        if not isinstance(self_data, dict):
            return

        alive_count = view.get("aliveCount", "?")

        if not self_data.get("isAlive", True):
            log.info("Agent DEAD. Alive remaining: %s. Waiting for game_ended.", alive_count)

            dashboard_state.update_agent(
                self.dashboard_key,
                {
                    "name": self.dashboard_name,
                    "status": "dead",
                    "hp": 0,
                    "ep": 0,
                    "maxHp": self_data.get("maxHp", 100),
                    "maxEp": self_data.get("maxEp", 10),
                    "alive_count": alive_count,
                    "last_action": "DEAD - waiting for game to end",
                    "enemies": [],
                    "region_items": [],
                },
            )

            dashboard_state.add_log(
                f"Agent DEAD. Alive remaining: {alive_count}",
                "warning",
                self.dashboard_key,
            )

            return

        hp = self_data.get("hp", "?")
        ep = self_data.get("ep", "?")
        region = view.get("currentRegion", {})
        region_name = region.get("name", "?") if isinstance(region, dict) else "?"

        log.info(
            "Status: HP=%s EP=%s Region=%s Alive=%s",
            hp,
            ep,
            region_name,
            alive_count,
        )

        dashboard_state.add_log(
            f"HP={hp} EP={ep} Region={region_name} Alive={alive_count}",
            "info",
            self.dashboard_key,
        )

        inventory = self_data.get("inventory", [])

        enemies = [
            agent
            for agent in view.get("visibleAgents", [])
            if isinstance(agent, dict)
            and agent.get("isAlive")
            and agent.get("id") != self_data.get("id")
        ]

        region_id = region.get("id", "") if isinstance(region, dict) else ""

        def unwrap_items(raw_items):
            result = []

            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue

                inner = entry.get("item")

                if isinstance(inner, dict):
                    item = dict(inner)
                    item["regionId"] = entry.get("regionId", "")
                    result.append(item)
                elif entry.get("id"):
                    result.append(entry)

            return result

        region_items = []

        if isinstance(region, dict) and region.get("items"):
            region_items = unwrap_items(region["items"])

        if not region_items:
            all_visible_items = unwrap_items(view.get("visibleItems", []))
            region_items = [
                item
                for item in all_visible_items
                if item.get("regionId") == region_id
            ]

        if not region_items:
            all_visible_items = unwrap_items(view.get("visibleItems", []))
            if all_visible_items:
                region_items = all_visible_items

        equipped = self_data.get("equippedWeapon")
        weapon_name = "fist"
        weapon_bonus = 0

        if equipped and isinstance(equipped, dict):
            weapon_name = equipped.get("typeId", "fist")

            try:
                from bot.strategy.brain import WEAPONS

                weapon_bonus = WEAPONS.get(weapon_name.lower(), {}).get("bonus", 0)
            except Exception:
                weapon_bonus = 0

        def item_label(item: dict):
            return (
                item.get("name")
                or item.get("typeId")
                or item.get("type")
                or item.get("itemType")
                or item.get("itemName")
                or item.get("label")
                or item.get("kind")
                or str(item.get("id", "?"))[:12]
            )

        def item_category(item: dict):
            return (
                item.get("category")
                or item.get("cat")
                or item.get("itemCategory")
                or item.get("type")
                or ""
            )

        dashboard_state.update_agent(
            self.dashboard_key,
            {
                "name": self.dashboard_name,
                "hp": hp,
                "ep": ep,
                "status": "playing",
                "maxHp": self_data.get("maxHp", 100),
                "maxEp": self_data.get("maxEp", 10),
                "atk": self_data.get("atk", 0),
                "def": self_data.get("def", 0),
                "weapon": weapon_name,
                "weapon_bonus": weapon_bonus,
                "kills": self_data.get("kills", 0),
                "region": region_name,
                "alive_count": alive_count,
                "inventory": [
                    {
                        "typeId": item.get("typeId", "?"),
                        "name": item_label(item),
                        "cat": item_category(item),
                    }
                    for item in inventory
                    if isinstance(item, dict)
                ],
                "enemies": [
                    {
                        "name": enemy.get("name", "?"),
                        "hp": enemy.get("hp", "?"),
                        "id": enemy.get("id", ""),
                    }
                    for enemy in enemies[:8]
                ],
                "region_items": [
                    {
                        "typeId": item.get("typeId", "?"),
                        "name": item_label(item),
                        "cat": item_category(item),
                    }
                    for item in region_items[:10]
                ],
            },
        )

        if self._map_just_used:
            self._map_just_used = False
            learn_from_map(view)
            log.info("Map knowledge updated. Death-zone tracking active.")

        _update_dz_knowledge(view)

        can_act = self.action_sender.can_send_cooldown_action()
        decision = decide_action(view, can_act)

        if decision is None:
            return

        action_type = decision["action"]
        action_data = decision.get("data", {})
        reason = decision.get("reason", "")

        if action_type in COOLDOWN_ACTIONS and not can_act:
            log.debug("Cooldown active. Skipping %s", action_type)
            return

        payload = self.action_sender.build_action(
            action_type,
            action_data,
            reason,
            action_type,
        )

        await self._send(payload)

        log.info("Action sent: %s | %s", action_type.upper(), reason)

        dashboard_state.update_agent(
            self.dashboard_key,
            {"last_action": f"{action_type}: {reason[:60]}"},
        )

        dashboard_state.add_log(
            f"{action_type}: {reason[:80]}",
            "info",
            self.dashboard_key,
        )

    async def _send(self, payload: dict):
        if self.ws is None:
            return

        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))

    async def _ping_loop(self):
        try:
            while self._running:
                await asyncio.sleep(15)

                if self.ws:
                    await self._send({"type": "ping"})

        except asyncio.CancelledError:
            pass

        except Exception as exc:
            log.debug("Ping loop error: %s", exc)

# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import json
import os
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote as url_quote
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin
from pynicotine.config import config
HITS_PLACEHOLDER = '{{ HIT_LIST_CONTENT }}'
BUTTON_PLACEHOLDER = '{{ BUTTON_LIST_CONTENT }}'
HIT_TEMPLATE = '<p class="log-item" data-hit="{hit_b64}"><b>Rendering...</b></p>\n'
INDENT = '  '
BUTTON_TEMPLATE = '<button class="header-btn" onclick="window.open(\'{path}/index.html\')">{label}</button>'
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * 60
SECONDS_PER_DAY = 86400
MIN_SCHEDULE_DELAY_SECONDS = 1
MIN_CLEANUP_DELAY_SECONDS = 60
TAIL_READ_CHUNK_BYTES = 8192

class Plugin(BasePlugin):
    LOG_FILE = 'hits.json'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plugin_dir = Path(__file__).resolve().parent
        self.log_file = self.plugin_dir / self.LOG_FILE
        self._lock = threading.RLock()
        self._cleanup_token = None
        self._flush_token = None
        self._flush_pending = False
        self._cleanup_running = False
        self._rotate_running = False
        defaults = {'enabled': True, 'monitor_list': [], 'log_wildcard': True, 'log_exact': True, 'log_user': True, 'expiry_days': 7, 'cleanup_interval_hours': 2, 'show_cleanup_log': True, 'auto_add_chat_users': False, 'auto_add_pm_users': False, 'auto_add_on_upload_queued': False, 'auto_add_on_wildcard_hit': False, 'auto_add_on_exact_hit': False, 'auto_add_on_user_resolve': False, 'auto_add_on_privileged_user': False, 'auto_add_on_room_user_join': False, 'auto_add_on_room_user_leave': False, 'ui_max_hits': 2000, 'wishlist_max_hits': 50000, 'wishlist_min_hits': 3, 'wishlist_max_rows': 300, 'log_all_matches': False, 'rotate_mb': 100, 'debug_log_incoming_searches': False}
        existing = getattr(self, 'settings', None)
        self.settings = dict(defaults)
        if isinstance(existing, dict):
            self.settings.update(existing)
        self.metasettings = {'enabled': {'description': 'Enable SearchStalk', 'type': 'bool'}, 'monitor_list': {'description': 'Monitored items (W:term, E:term, U:UserName)', 'type': 'list string'}, 'log_wildcard': {'description': 'Log WILDCARD Hits', 'type': 'bool'}, 'log_exact': {'description': 'Log EXACT Hits', 'type': 'bool'}, 'log_user': {'description': 'Log USER Hits', 'type': 'bool'}, 'expiry_days': {'description': 'Days To Retain Hits (0 = forever)', 'type': 'int', 'minimum': 0, 'maximum': 1000}, 'cleanup_interval_hours': {'description': 'Cleanup Interval (hours)', 'type': 'int', 'minimum': 1, 'maximum': 168}, 'show_cleanup_log': {'description': 'Show Cleanup Log', 'type': 'bool'}, 'auto_add_chat_users': {'description': 'Auto-add On Public Chat', 'type': 'bool'}, 'auto_add_pm_users': {'description': 'Auto-add On Private Message', 'type': 'bool'}, 'auto_add_on_upload_queued': {'description': 'Auto-add On Upload Queue', 'type': 'bool'}, 'auto_add_on_wildcard_hit': {'description': 'Auto-add On Wildcard', 'type': 'bool'}, 'auto_add_on_exact_hit': {'description': 'Auto-add On Exact', 'type': 'bool'}, 'auto_add_on_user_resolve': {'description': 'Auto-add On User Resolve', 'type': 'bool'}, 'auto_add_on_privileged_user': {'description': 'Auto-add Privileged Users', 'type': 'bool'}, 'auto_add_on_room_user_join': {'description': 'Auto-add User When They Join a Chatroom', 'type': 'bool'}, 'auto_add_on_room_user_leave': {'description': 'Auto-add User When They Leave a Chatroom', 'type': 'bool'}, 'ui_max_hits': {'description': 'UI Max Hits (0 = unlimited)', 'type': 'int', 'minimum': 0, 'maximum': 200000}, 'wishlist_max_hits': {'description': 'Wishlist Analysis Window (0 = unlimited)', 'type': 'int', 'minimum': 0, 'maximum': 2000000}, 'wishlist_min_hits': {'description': 'Wishlist Threshold (min hits)', 'type': 'int', 'minimum': 1, 'maximum': 1000}, 'wishlist_max_rows': {'description': 'Wishlist Max Rows', 'type': 'int', 'minimum': 10, 'maximum': 5000}, 'log_all_matches': {'description': 'Log ALL matching rules per search', 'type': 'bool'}, 'rotate_mb': {'description': 'Rotate hits.json at (MB, 0=off)', 'type': 'int', 'minimum': 0, 'maximum': 5000}, 'debug_log_incoming_searches': {'description': 'DEBUG: Log every incoming search notification', 'type': 'bool'}}
        self._watch_rules = {}
        self.commands = {'ss_wild': {'callback': self.cmd_watch, 'description': 'Add wildcard watch'}, 'ss_exact': {'callback': self.cmd_watche, 'description': 'Add exact watch'}, 'ss_user': {'callback': self.cmd_watchu, 'description': 'Add user watch'}, 'ss_remove': {'callback': self.cmd_unwatch, 'description': 'Remove watch'}, 'ss_list': {'callback': self.cmd_watchlist, 'description': 'Show watches'}, 'ss_ui': {'callback': self.cmd_stalkui, 'description': 'Open SearchStalk UI'}, 'ss_wishlist': {'callback': self.cmd_wishlist, 'description': 'Open Wishlist UI'}}

    def _plugin_key(self) -> str:
        k = getattr(self, 'internal_name', None)
        if isinstance(k, str) and k.strip():
            return k.strip()
        k = getattr(self, 'name', None)
        if isinstance(k, str) and k.strip():
            return k.strip()
        try:
            return self.plugin_dir.name
        except Exception:
            pass
        k = getattr(self, '__module__', None)
        if isinstance(k, str) and k.strip():
            return k.strip()
        return self.__class__.__name__

    def _coerce_bool(self, v, default=False):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ('1', 'true', 'yes', 'y', 'on', 'enabled', 'enable'):
                return True
            if s in ('0', 'false', 'no', 'n', 'off', 'disabled', 'disable'):
                return False
        return default

    def _coerce_int(self, v, default=0):
        if isinstance(v, bool):
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v.strip(), 10)
            except ValueError:
                return default
        return default

    def _clamp_int(self, value, minimum=None, maximum=None):
        if minimum is not None:
            value = max(value, int(minimum))
        if maximum is not None:
            value = min(value, int(maximum))
        return value

    def _sanitize_settings(self):
        with self._lock:
            for key in ('enabled', 'log_wildcard', 'log_exact', 'log_user', 'show_cleanup_log', 'auto_add_chat_users', 'auto_add_pm_users', 'auto_add_on_upload_queued', 'auto_add_on_wildcard_hit', 'auto_add_on_exact_hit', 'auto_add_on_user_resolve', 'auto_add_on_privileged_user', 'auto_add_on_room_user_join', 'auto_add_on_room_user_leave', 'log_all_matches', 'debug_log_incoming_searches'):
                self.settings[key] = self._coerce_bool(self.settings.get(key), bool(self.settings.get(key, False)))
            self.settings['expiry_days'] = self._clamp_int(self._coerce_int(self.settings.get('expiry_days'), 7), 0, 1000)
            self.settings['cleanup_interval_hours'] = self._clamp_int(self._coerce_int(self.settings.get('cleanup_interval_hours'), 6), 1, 168)
            self.settings['ui_max_hits'] = self._clamp_int(self._coerce_int(self.settings.get('ui_max_hits'), 2000), 0, 200000)
            self.settings['wishlist_max_hits'] = self._clamp_int(self._coerce_int(self.settings.get('wishlist_max_hits'), 50000), 0, 2000000)
            self.settings['wishlist_min_hits'] = self._clamp_int(self._coerce_int(self.settings.get('wishlist_min_hits'), 3), 1, 1000)
            self.settings['wishlist_max_rows'] = self._clamp_int(self._coerce_int(self.settings.get('wishlist_max_rows'), 300), 10, 5000)
            self.settings['rotate_mb'] = self._clamp_int(self._coerce_int(self.settings.get('rotate_mb'), 100), 0, 5000)
            ml = self.settings.get('monitor_list', [])
            if not isinstance(ml, list):
                ml = []
            cleaned = []
            for item in ml:
                if isinstance(item, str):
                    s = item.strip()
                    if s:
                        cleaned.append(s)
            self.settings['monitor_list'] = cleaned

    def init(self):
        self._sanitize_settings()
        self._rebuild_rules_from_settings()
        self._schedule_cleanup(delay=15)

    def loaded_notification(self):
        try:
            self.init()
        except Exception as e:
            self.log(f'SearchStalk: init failed during loaded_notification: {type(e).__name__}: {e}')

    def settings_changed(self, before, after, change):
        try:
            self._sanitize_settings()
            self.save_settings()
            self._rebuild_rules_from_settings()
            self._schedule_cleanup(delay=15)
        except Exception as e:
            self.log(f'SearchStalk: settings_changed handler failed: {type(e).__name__}: {e}')

    def disable(self):
        with self._lock:
            if self._cleanup_token:
                try:
                    events.cancel_scheduled(self._cleanup_token)
                except Exception:
                    pass
                self._cleanup_token = None
            if self._flush_token:
                try:
                    events.cancel_scheduled(self._flush_token)
                except Exception:
                    pass
                self._flush_token = None
            self._flush_pending = False
            self._cleanup_running = False
            self._rotate_running = False

    def _rebuild_rules_from_settings(self):
        with self._lock:
            self._watch_rules.clear()
            for item in self.settings.get('monitor_list', []):
                raw = item.strip()
                if ':' not in raw:
                    continue
                mode, value = raw.split(':', 1)
                mode = (mode or '').strip().upper()
                value = (value or '').strip()
                if not value or mode not in ('W', 'E', 'U'):
                    continue
                if mode in ('W', 'E'):
                    self._watch_rules[value.lower()] = mode
                else:
                    self._watch_rules[value] = 'U'
            self._sync_to_settings_locked()

    def _sync_to_settings_locked(self):
        items = []
        for key, mode in self._watch_rules.items():
            items.append(f'{mode}:{key}')
        self.settings['monitor_list'] = sorted(items, key=str.lower)

    def save_settings(self):
        self._sanitize_settings()
        try:
            plugin_key = self._plugin_key()
            config.sections.setdefault('plugins', {})
            config.sections['plugins'][plugin_key] = dict(self.settings)
            write_fn = getattr(config, 'write_configuration', None) or getattr(config, 'write_config', None) or getattr(config, 'write', None)
            if not callable(write_fn):
                raise AttributeError('Config object has no write_configuration/write_config/write method')
            write_fn()
        except OSError as e:
            self.log(f'SearchStalk: save_settings failed (OS error): {e}')
        except Exception as e:
            self.log(f'SearchStalk: save_settings failed (unexpected): {type(e).__name__}: {e}')

    def _schedule_flush_settings(self, delay_seconds=1):
        with self._lock:
            self._flush_pending = True
            if self._flush_token:
                try:
                    events.cancel_scheduled(self._flush_token)
                except Exception:
                    pass
                self._flush_token = None
            delay_seconds = max(MIN_SCHEDULE_DELAY_SECONDS, int(delay_seconds))
            self._flush_token = events.schedule(delay=delay_seconds, callback=self._flush_settings_worker)

    def _flush_settings_worker(self):
        with self._lock:
            self._flush_token = None
            if not self._flush_pending:
                return
            self._flush_pending = False
        try:
            self.save_settings()
        except OSError as e:
            self.log(f'SearchStalk: save_settings failed (OS error): {e}')
        except Exception as e:
            self.log(f'SearchStalk: save_settings failed (unexpected): {type(e).__name__}: {e}')

    def _schedule_cleanup(self, delay=None):
        with self._lock:
            if self._cleanup_token:
                try:
                    events.cancel_scheduled(self._cleanup_token)
                except Exception:
                    pass
                self._cleanup_token = None
            days = int(self.settings.get('expiry_days', 0))
            hours = int(self.settings.get('cleanup_interval_hours', 6))
        if days <= 0:
            return
        if delay is None:
            delay = max(1, hours) * SECONDS_PER_HOUR
            delay = max(MIN_CLEANUP_DELAY_SECONDS, int(delay))
        else:
            delay = max(MIN_SCHEDULE_DELAY_SECONDS, int(delay))
        with self._lock:
            self._cleanup_token = events.schedule(delay=delay, callback=self._cleanup_worker)

    def _cleanup_worker(self):
        with self._lock:
            if self._cleanup_running:
                return
            self._cleanup_running = True
        threading.Thread(target=self._cleanup_thread, name='SearchStalkCleanup', daemon=True).start()

    def _cleanup_thread(self):
        try:
            removed, total, committed = self._prune_hits_file_streaming()
            with self._lock:
                show = bool(self.settings.get('show_cleanup_log', False))
            if show and total:
                if committed:
                    self.log(f'Cleanup: pruned {removed}/{total} expired hits')
                else:
                    self.log(f'Cleanup: skipped commit (file changed); would prune {removed}/{total}')
        finally:
            with self._lock:
                self._cleanup_running = False
            self._schedule_cleanup()

    def _prune_hits_file_streaming(self):
        with self._lock:
            days = int(self.settings.get('expiry_days', 0))
        if days <= 0:
            return (0, 0, False)
        if not self.log_file.exists():
            return (0, 0, False)
        try:
            st0 = self.log_file.stat()
            start_size = st0.st_size
            start_mtime = getattr(st0, 'st_mtime_ns', int(st0.st_mtime * 1000000000))
        except OSError:
            return (0, 0, False)
        now = time.time()
        removed = 0
        total = 0
        tmp_path = self.log_file.with_suffix('.json.tmp')
        try:
            with self.log_file.open('r', encoding='utf-8', errors='replace') as src, tmp_path.open('w', encoding='utf-8') as dst:
                for line in src:
                    total += 1
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        removed += 1
                        continue
                    expiry = obj.get('expiry', 0) or 0
                    if expiry == 0 or float(expiry) > now:
                        dst.write(json.dumps(obj, ensure_ascii=False) + '\n')
                    else:
                        removed += 1
        except OSError:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return (0, total, False)
        if removed <= 0:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return (0, total, False)
        try:
            st1 = self.log_file.stat()
            end_size = st1.st_size
            end_mtime = getattr(st1, 'st_mtime_ns', int(st1.st_mtime * 1000000000))
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return (0, total, False)
        if end_size != start_size or end_mtime != start_mtime:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return (removed, total, False)
        try:
            tmp_path.replace(self.log_file)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return (0, total, False)
        return (removed, total, True)

    def _maybe_schedule_rotate(self):
        with self._lock:
            rotate_mb = int(self.settings.get('rotate_mb', 0))
            if rotate_mb <= 0:
                return
            if self._rotate_running:
                return
        try:
            size = self.log_file.stat().st_size if self.log_file.exists() else 0
        except OSError:
            return
        if size >= rotate_mb * 1024 * 1024:
            with self._lock:
                if self._rotate_running:
                    return
                self._rotate_running = True
            threading.Thread(target=self._rotate_thread, name='SearchStalkRotate', daemon=True).start()

    def _rotate_thread(self):
        try:
            with self._lock:
                rotate_mb = int(self.settings.get('rotate_mb', 0))
                if rotate_mb <= 0:
                    return
                if not self.log_file.exists():
                    return
                try:
                    size = self.log_file.stat().st_size
                except OSError:
                    return
                if size < rotate_mb * 1024 * 1024:
                    return
                ts = datetime.now().strftime('%Y%m%d-%H%M%S')
                rotated = self.plugin_dir / f'hits-{ts}.jsonl'
                self.log_file.replace(rotated)
                self.log(f'Log rotated: {self.LOG_FILE} -> {rotated.name}')
        finally:
            with self._lock:
                self._rotate_running = False

    def _auto_add_user_watch(self, user):
        if not user or not isinstance(user, str):
            return False
        user = user.strip()
        if not user:
            return False
        with self._lock:
            if self._watch_rules.get(user) == 'U':
                return False
            self._watch_rules[user] = 'U'
            self._sync_to_settings_locked()
        self._schedule_flush_settings(delay_seconds=1)
        return True

    def upload_queued_notification(self, user, *_):
        if self.settings.get('auto_add_on_upload_queued', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (upload queued)')

    def incoming_public_chat_event(self, room, user, line):
        if self.settings.get('auto_add_chat_users', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (public chat)')

    def incoming_private_chat_event(self, user, line):
        if self.settings.get('auto_add_pm_users', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (private message)')

    def user_resolve_notification(self, user, *_):
        if self.settings.get('auto_add_on_user_resolve', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (user resolve)')

    def user_status_notification(self, user, status, privileged):
        if privileged and self.settings.get('auto_add_on_privileged_user', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (privileged)')

    def user_join_chatroom_notification(self, room, user):
        if not self.settings.get('auto_add_on_room_user_join', False):
            return
        if self._auto_add_user_watch(user):
            self.log(f"Auto-added U:{user} (joined room '{room}')")

    def user_leave_chatroom_notification(self, room, user):
        if not self.settings.get('auto_add_on_room_user_leave', False):
            return
        if self._auto_add_user_watch(user):
            self.log(f"Auto-added U:{user} (left room '{room}')")

    @staticmethod
    def _mode_priority(mode: str) -> int:
        return {'U': 0, 'E': 1, 'W': 2}.get(mode, 9)

    def distrib_search_notification(self, searchterm, user, token):
        if not self.settings.get('enabled', True):
            return
        searchterm = (searchterm or '').strip()
        user = (user or '').strip()
        if not searchterm or not user:
            return
        if self.settings.get('debug_log_incoming_searches', False):
            st = searchterm if len(searchterm) <= 200 else searchterm[:200] + '…'
            self.log(f"[DEBUG] distrib_search_notification: user='{user}' term='{st}'")
        search_lc = searchterm.lower()
        with self._lock:
            rules = list(self._watch_rules.items())
            log_all = bool(self.settings.get('log_all_matches', False))
        matches = []
        for key, mode in rules:
            if mode == 'W' and key in search_lc or (mode == 'E' and key == search_lc) or (mode == 'U' and key == user):
                matches.append((mode, key))
        if not matches:
            return
        matches.sort(key=lambda mk: (self._mode_priority(mk[0]), str(mk[1]).lower()))
        if log_all:
            for mode, key in matches:
                self._handle_match(user, searchterm, key, mode)
        else:
            mode, key = matches[0]
            self._handle_match(user, searchterm, key, mode)

    def _handle_match(self, user, searchterm, trigger, mode):
        timestamp = datetime.now().strftime('%m/%d/%Y %I:%M:%S %p')
        with self._lock:
            days = int(self.settings.get('expiry_days', 0))
        expiry = 0
        if days > 0:
            expiry = int(time.time() + days * SECONDS_PER_DAY)
        entry = {'time': timestamp, 'user': user, 'mode': mode, 'trigger': trigger, 'query': searchterm, 'expiry': expiry}
        self._append_hit(entry)
        if mode == 'W' and self.settings.get('log_wildcard', True) or (mode == 'E' and self.settings.get('log_exact', True)) or (mode == 'U' and self.settings.get('log_user', True)):
            self.log(f"[{mode}] '{user}': '{searchterm}'")
        if mode == 'W' and self.settings.get('auto_add_on_wildcard_hit', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (wildcard hit)')
        elif mode == 'E' and self.settings.get('auto_add_on_exact_hit', False):
            if self._auto_add_user_watch(user):
                self.log(f'Auto-added U:{user} (exact hit)')

    def _append_hit(self, entry: dict):
        try:
            line = json.dumps(entry, ensure_ascii=False) + '\n'
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                with self.log_file.open('a', encoding='utf-8') as f:
                    f.write(line)
        except OSError:
            return
        self._maybe_schedule_rotate()

    def cmd_watch(self, arg='', **_):
        term = (arg or '').strip()
        if not term:
            self.output('Usage: /ss_wild <term>')
            return
        key = term.lower()
        with self._lock:
            prev = self._watch_rules.get(key)
            self._watch_rules[key] = 'W'
            self._sync_to_settings_locked()
        self._schedule_flush_settings(delay_seconds=1)
        if prev == 'W':
            self.output(f'Already watching W:{key}')
        elif prev:
            self.output(f'Updated {prev}:{key} -> W:{key}')
        else:
            self.output(f'Added W:{key}')

    def cmd_watche(self, arg='', **_):
        term = (arg or '').strip()
        if not term:
            self.output('Usage: /ss_exact <term>')
            return
        key = term.lower()
        with self._lock:
            prev = self._watch_rules.get(key)
            self._watch_rules[key] = 'E'
            self._sync_to_settings_locked()
        self._schedule_flush_settings(delay_seconds=1)
        if prev == 'E':
            self.output(f'Already watching E:{key}')
        elif prev:
            self.output(f'Updated {prev}:{key} -> E:{key}')
        else:
            self.output(f'Added E:{key}')

    def cmd_watchu(self, arg='', **_):
        username = (arg or '').strip()
        if not username:
            self.output('Usage: /ss_user <UserName>')
            return
        with self._lock:
            prev = self._watch_rules.get(username)
            self._watch_rules[username] = 'U'
            self._sync_to_settings_locked()
        self._schedule_flush_settings(delay_seconds=1)
        if prev == 'U':
            self.output(f'Already watching U:{username}')
        elif prev:
            self.output(f'Updated {prev}:{username} -> U:{username}')
        else:
            self.output(f'Added U:{username}')

    def cmd_unwatch(self, arg='', **_):
        raw = (arg or '').strip()
        if not raw:
            self.output('Usage: /ss_remove <term|user>  OR  /ss_remove W:<term> | E:<term> | U:<UserName>')
            return
        if ':' in raw:
            mode, value = raw.split(':', 1)
            mode = (mode or '').strip().upper()
            value = (value or '').strip()
            if mode not in ('W', 'E', 'U') or not value:
                self.output('Usage: /ss_remove W:<term> | E:<term> | U:<UserName>')
                return
            key = value.lower() if mode in ('W', 'E') else value
            with self._lock:
                if self._watch_rules.get(key) != mode:
                    self.output(f'No watch found for {mode}:{key}')
                    return
                self._watch_rules.pop(key, None)
                self._sync_to_settings_locked()
            self._schedule_flush_settings(delay_seconds=1)
            self.output(f'Removed {mode}:{key}')
            return
        candidates = []
        with self._lock:
            if raw in self._watch_rules:
                candidates.append((self._watch_rules[raw], raw))
            low = raw.lower()
            if low in self._watch_rules and low != raw:
                candidates.append((self._watch_rules[low], low))
        uniq = []
        seen = set()
        for m, k in candidates:
            if (m, k) not in seen:
                uniq.append((m, k))
                seen.add((m, k))
        candidates = uniq
        if not candidates:
            self.output(f"No watch found for '{raw}'")
            return
        if len(candidates) > 1:
            opts = ', '.join([f'{m}:{k}' for m, k in candidates])
            self.output(f"Ambiguous remove for '{raw}'. Matches: {opts}. Use /ss_remove <MODE>:<value> e.g. /ss_remove {candidates[0][0]}:{candidates[0][1]}")
            return
        mode, key = candidates[0]
        with self._lock:
            self._watch_rules.pop(key, None)
            self._sync_to_settings_locked()
        self._schedule_flush_settings(delay_seconds=1)
        self.output(f'Removed {mode}:{key}')

    def cmd_watchlist(self, *_, **__):
        with self._lock:
            if not self._watch_rules:
                self.output('No watches set.')
                return
            wild = sorted([k for k, v in self._watch_rules.items() if v == 'W'])
            exact = sorted([k for k, v in self._watch_rules.items() if v == 'E'])
            users = sorted([k for k, v in self._watch_rules.items() if v == 'U'])
        parts = []
        parts.extend([f'W:{k}' for k in wild])
        parts.extend([f'E:{k}' for k in exact])
        parts.extend([f'U:{k}' for k in users])
        self.output(', '.join(parts))

    def cmd_stalkui(self, *_, **__):
        self._render_ui('web-ui', 'searchstalk.html', open_browser=True)

    def cmd_wishlist(self, *_, **__):
        self._render_wishlist_ui(open_browser=True)

    def _build_buttons_html(self, ui_dir: Path) -> str:
        buttons = []
        for label in self.get_web_ui_buttons(ui_dir):
            buttons.append(BUTTON_TEMPLATE.format(path=url_quote(label), label=html_escape(label)))
        if not buttons:
            return ''
        return ('\n' + INDENT * 3).join(buttons)

    def _iter_hits_tail(self, max_lines: int):
        if not self.log_file.exists():
            return
        if max_lines <= 0:
            skipped = 0
            try:
                with self.log_file.open('r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        s = line.strip()
                        if not s:
                            continue
                        try:
                            yield json.loads(s)
                        except json.JSONDecodeError:
                            skipped += 1
            except OSError:
                return
            if skipped:
                self.log(f'UI: skipped {skipped} malformed hit lines')
            return
        skipped = 0
        try:
            with self.log_file.open('rb') as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                buf = b''
                lines = []
                while pos > 0 and len(lines) <= max_lines:
                    step = TAIL_READ_CHUNK_BYTES if pos >= TAIL_READ_CHUNK_BYTES else pos
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step) + buf
                    parts = buf.split(b'\n')
                    buf = parts[0]
                    lines = parts[1:] + lines
                raw_lines = []
                for raw in lines[-max_lines:]:
                    raw = raw.strip()
                    if raw:
                        raw_lines.append(raw)
            for raw in raw_lines:
                try:
                    yield json.loads(raw.decode('utf-8', errors='replace'))
                except json.JSONDecodeError:
                    skipped += 1
        except OSError:
            return
        if skipped:
            self.log(f'UI: skipped {skipped} malformed hit lines')

    def _render_ui(self, ui_dir_name, output_name, open_browser=False):
        ui_dir = self.plugin_dir / ui_dir_name
        template = ui_dir / 'template.html'
        output = ui_dir / output_name
        if not template.exists():
            self.log(f'{ui_dir_name}: template not found at {template}')
            return
        try:
            template_html = template.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            self.log(f'{ui_dir_name}: failed to read template: {e}')
            return
        max_hits = int(self.settings.get('ui_max_hits', 2000))
        rows = []
        for hit in self._iter_hits_tail(max_hits):
            try:
                b64 = base64.b64encode(json.dumps(hit, ensure_ascii=False).encode('utf-8')).decode('ascii')
                rows.append(HIT_TEMPLATE.format(hit_b64=b64))
            except (TypeError, ValueError):
                continue
        hits_html = ''.join(rows) if rows else '<p><b>No hits yet.</b></p>'
        buttons_html = self._build_buttons_html(ui_dir)
        final = template_html.replace(HITS_PLACEHOLDER, hits_html).replace(BUTTON_PLACEHOLDER, buttons_html)
        try:
            output.write_text(final, encoding='utf-8')
        except OSError as e:
            self.log(f'{ui_dir_name}: failed to write UI: {e}')
            return
        if open_browser:
            try:
                webbrowser.open(output.as_uri())
            except OSError as e:
                self.log(f'{ui_dir_name}: failed to open browser: {e}')

    @staticmethod
    def _normalize_query(q):
        return ' '.join((q or '').strip().lower().split())

    def _render_wishlist_ui(self, open_browser=False):
        wishlist_ui = self.plugin_dir / 'wishlist-ui'
        template = wishlist_ui / 'template.html'
        output = wishlist_ui / 'wishlist.html'
        if not template.exists():
            self.log(f'wishlist-ui: template not found at {template}')
            return
        try:
            template_html = template.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            self.log(f'wishlist-ui: failed to read template: {e}')
            return
        max_hits = int(self.settings.get('wishlist_max_hits', 50000))
        min_hits = int(self.settings.get('wishlist_min_hits', 3))
        max_rows = int(self.settings.get('wishlist_max_rows', 300))
        counts = defaultdict(int)
        first_seen = {}
        for hit in self._iter_hits_tail(max_hits):
            user = hit.get('user')
            query = hit.get('query')
            if not user or not query:
                continue
            norm = self._normalize_query(query)
            key = (user, norm)
            counts[key] += 1
            if key not in first_seen:
                first_seen[key] = hit
        sorted_items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0].lower(), kv[0][1].lower()))
        rows = []
        emitted = 0
        for (user, norm), count in sorted_items:
            if count < min_hits:
                continue
            if emitted >= max_rows:
                break
            base = first_seen[user, norm]
            wishlist_hit = {'time': f'WISHLIST ({count} hits)', 'user': user, 'mode': 'WISHLIST', 'trigger': 'wishlist', 'query': base.get('query', ''), 'expiry': 0}
            try:
                b64 = base64.b64encode(json.dumps(wishlist_hit, ensure_ascii=False).encode('utf-8')).decode('ascii')
                rows.append(HIT_TEMPLATE.format(hit_b64=b64))
                emitted += 1
            except (TypeError, ValueError):
                continue
        hits_html = ''.join(rows) if rows else '<p><b>No wishlist items yet.</b></p>'
        buttons_html = self._build_buttons_html(wishlist_ui)
        final = template_html.replace(HITS_PLACEHOLDER, hits_html).replace(BUTTON_PLACEHOLDER, buttons_html)
        try:
            output.write_text(final, encoding='utf-8')
        except OSError as e:
            self.log(f'wishlist-ui: failed to write UI: {e}')
            return
        if open_browser:
            try:
                webbrowser.open(output.as_uri())
            except OSError as e:
                self.log(f'wishlist-ui: failed to open browser: {e}')

    @staticmethod
    def get_web_ui_buttons(target_path: Path):
        ignored_dirs = {'fonts', 'images', 'css', 'js'}
        if not target_path.is_dir():
            return []
        try:
            return sorted([d.name for d in target_path.iterdir() if d.is_dir() and (not d.name.startswith('.')) and (d.name not in ignored_dirs) and (d / 'index.html').is_file()], key=str.lower)
        except (PermissionError, FileNotFoundError, OSError):
            return []

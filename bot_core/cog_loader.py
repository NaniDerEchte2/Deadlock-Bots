from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from discord.ext import commands


class CogLoaderMixin:
    """Cog-Discovery, Blocklist und Reload-Helfer."""

    def normalize_namespace(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ValueError("namespace must not be empty")

        normalized = text.replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("namespace must not be empty")

        if "." in normalized and "/" not in normalized:
            parts = [segment for segment in normalized.split(".") if segment]
        else:
            parts = [segment for segment in normalized.split("/") if segment]

        if not parts:
            raise ValueError("namespace must not be empty")

        if parts[0] != "cogs":
            parts.insert(0, "cogs")

        return ".".join(parts)

    def _load_blocklist(self) -> None:
        try:
            if not self.blocklist_path.exists():
                self.blocked_namespaces = set()
                return
            data = json.loads(self.blocklist_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("blocklist must be a list")
            loaded = set()
            for item in data:
                try:
                    loaded.add(self.normalize_namespace(str(item)))
                except Exception:
                    continue
            self.blocked_namespaces = loaded
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Konnte Blockliste nicht laden (%s): %s", self.blocklist_path, e
            )
            self.blocked_namespaces = set()

    def _save_blocklist(self) -> None:
        try:
            self.blocklist_path.parent.mkdir(parents=True, exist_ok=True)
            self.blocklist_path.write_text(
                json.dumps(sorted(self.blocked_namespaces)),
                encoding="utf-8",
            )
        except Exception as e:
            logging.getLogger(__name__).error(
                "Blockliste konnte nicht gespeichert werden (%s): %s",
                self.blocklist_path,
                e,
            )

    def is_namespace_blocked(
        self, namespace: str, *, assume_normalized: bool = False
    ) -> bool:
        try:
            target = (
                namespace if assume_normalized else self.normalize_namespace(namespace)
            )
        except ValueError:
            return False
        for blocked in self.blocked_namespaces:
            if target == blocked or target.startswith(f"{blocked}."):
                return True
        return False

    async def block_namespace(self, namespace: str) -> Dict[str, Any]:
        normalized = self.normalize_namespace(namespace)
        if normalized in self.blocked_namespaces:
            return {"namespace": normalized, "changed": False, "unloaded": {}}

        self.blocked_namespaces.add(normalized)
        self._save_blocklist()

        to_unload = [
            ext for ext in list(self.extensions.keys()) if ext.startswith(normalized)
        ]
        unload_results: Dict[str, str] = {}
        if to_unload:
            unload_results = await self.unload_many(to_unload)

        for key in list(self.cog_status.keys()):
            if key == normalized or key.startswith(f"{normalized}."):
                self.cog_status[key] = "blocked"
        if normalized not in self.cog_status:
            self.cog_status[normalized] = "blocked"

        self.auto_discover_cogs()
        return {"namespace": normalized, "changed": True, "unloaded": unload_results}

    async def unblock_namespace(self, namespace: str) -> Dict[str, Any]:
        normalized = self.normalize_namespace(namespace)
        if normalized not in self.blocked_namespaces:
            return {"namespace": normalized, "changed": False}

        self.blocked_namespaces.discard(normalized)
        self._save_blocklist()

        for key in list(self.cog_status.keys()):
            if key == normalized or key.startswith(f"{normalized}."):
                self.cog_status[key] = "unloaded"

        self.auto_discover_cogs()
        return {"namespace": normalized, "changed": True}

    def _should_exclude(self, module_path: str) -> bool:
        default_excludes = {
            "",
        }
        env_ex = (os.getenv("COG_EXCLUDE") or "").strip()
        for item in [x.strip() for x in env_ex.split(",") if x.strip()]:
            default_excludes.add(item)
        only = {
            x.strip() for x in (os.getenv("COG_ONLY") or "").split(",") if x.strip()
        }
        if only:
            return module_path not in only
        if module_path in default_excludes:
            return True
        if self.is_namespace_blocked(module_path, assume_normalized=True):
            return True
        return False

    def auto_discover_cogs(self):
        try:
            importlib.invalidate_caches()
            if not self.cogs_dir.exists():
                logging.warning(f"Cogs directory not found: {self.cogs_dir}")
                return

            discovered: List[str] = []
            pkg_dirs_with_setup: List[Path] = []

            # Pass 1: Paket-Cogs mit setup() in __init__.py
            for init_file in self.cogs_dir.rglob("__init__.py"):
                if any(part == "__pycache__" for part in init_file.parts):
                    continue
                try:
                    content = init_file.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    logging.warning(f"⚠️ Error reading {init_file}: {e}")
                    continue
                has_setup = ("async def setup(" in content) or ("def setup(" in content)
                if not has_setup:
                    continue
                rel = init_file.relative_to(self.cogs_dir.parent)
                module_path = ".".join(rel.parts[:-1])
                if self._should_exclude(module_path):
                    logging.debug(f"🚫 Excluded cog (package): {module_path}")
                    continue
                discovered.append(module_path)
                pkg_dirs_with_setup.append(init_file.parent)
                logging.debug(f"🔍 Auto-discovered package cog: {module_path}")

            # Pass 2: Einzelne .py
            for cog_file in self.cogs_dir.rglob("*.py"):
                if cog_file.name == "__init__.py":
                    continue
                if any(part == "__pycache__" for part in cog_file.parts):
                    continue
                if any(
                    cog_file.is_relative_to(pkg_dir) for pkg_dir in pkg_dirs_with_setup
                ):
                    continue
                try:
                    content = cog_file.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    logging.warning(f"⚠️ Error checking {cog_file.name}: {e}")
                    continue
                has_setup = ("async def setup(" in content) or ("def setup(" in content)
                if not has_setup:
                    logging.debug(f"⏭️ Skipped {cog_file}: no setup() found")
                    continue
                rel = cog_file.relative_to(self.cogs_dir.parent)
                module_path = ".".join(rel.with_suffix("").parts)
                if self._should_exclude(module_path):
                    logging.debug(f"🚫 Excluded cog: {module_path}")
                    continue
                discovered.append(module_path)
                logging.debug(f"🔍 Auto-discovered cog: {module_path}")

            self.cogs_list = sorted(set(discovered))
            logging.info(
                f"✅ Auto-discovery complete: {len(self.cogs_list)} cogs found"
            )

            for key in list(self.cog_status.keys()):
                if self.is_namespace_blocked(key, assume_normalized=True):
                    self.cog_status[key] = "blocked"

        except Exception as e:
            logging.error(f"❌ Error during cog auto-discovery: {e}")
            logging.error("❌ CRITICAL: No cogs will be loaded! Check cogs/ directory")
            self.cogs_list = []

    def resolve_cog_identifier(
        self, identifier: str | None
    ) -> Tuple[Optional[str], List[str]]:
        if not identifier:
            return None, []

        ident = identifier.strip()
        if not ident:
            return None, []

        if ident in self.extensions:
            return ident, []
        if ident in self.cogs_list:
            return ident, []
        if ident.startswith("cogs."):
            return ident, []

        matches = [c for c in self.cogs_list if c.endswith(f".{ident}")]
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return None, matches

        prefixed = f"cogs.{ident}"
        if prefixed in self.cogs_list or prefixed in self.extensions:
            return prefixed, []

        return None, []

    async def load_all_cogs(self):
        logging.info("Loading all cogs in parallel...")

        async def load_single_cog(cog_name: str):
            try:
                self._purge_namespace_modules(cog_name)
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = "loaded"
                logging.info(f"✅ Loaded cog: {cog_name}")
                return True, cog_name, None
            except Exception as e:
                self.cog_status[cog_name] = f"error: {str(e)[:100]}"
                logging.error(f"❌ Failed to load cog {cog_name}: {e}")
                return False, cog_name, e

        tasks = [load_single_cog(c) for c in self.cogs_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = 0
        for r in results:
            if isinstance(r, tuple) and r[0]:
                ok += 1
            elif isinstance(r, Exception):
                logging.error(f"❌ Unexpected error during cog loading: {r}")

        logging.info(
            f"Parallel cog loading completed: {ok}/{len(self.cogs_list)} successful"
        )
        await self.update_presence()

    async def reload_all_cogs_with_discovery(self):
        try:
            unload_results = []
            loaded_extensions = [
                ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")
            ]

            for ext_name in loaded_extensions:
                try:
                    await asyncio.wait_for(
                        self.unload_extension(ext_name),
                        timeout=self.per_cog_unload_timeout,
                    )
                    self._purge_namespace_modules(ext_name)
                    unload_results.append(f"✅ Unloaded: {ext_name}")
                    self.cog_status[ext_name] = "unloaded"
                    logging.info(f"Unloaded extension: {ext_name}")
                except asyncio.TimeoutError:
                    unload_results.append(f"⏱️ Timeout unloading {ext_name}")
                    logging.error(f"Timeout unloading extension {ext_name}")
                except Exception as e:
                    unload_results.append(
                        f"❌ Error unloading {ext_name}: {str(e)[:50]}"
                    )
                    logging.error(f"Error unloading {ext_name}: {e}")

            old_count = len(self.cogs_list)
            self.auto_discover_cogs()
            new_count = len(self.cogs_list)

            self.cog_status = {}
            await self.load_all_cogs()

            loaded_count = len([s for s in self.cog_status.values() if s == "loaded"])
            await self.update_presence()

            summary = {
                "unloaded": len(unload_results),
                "discovered": new_count,
                "loaded": loaded_count,
                "new_cogs": new_count - old_count,
                "unload_details": unload_results,
            }
            return True, summary

        except Exception as e:
            logging.error(f"Error during full cog reload: {e}")
            return False, f"Error: {str(e)}"

    def _purge_namespace_modules(self, namespace: str) -> None:
        """Ensure that a namespace will be freshly imported on the next load."""

        try:
            importlib.invalidate_caches()
        except Exception as e:
            logging.debug("Failed to invalidate import caches: %s", e)

        trimmed = namespace.rstrip(".")
        if not trimmed:
            return

        removed = []
        for mod_name in list(sys.modules.keys()):
            if mod_name == trimmed or mod_name.startswith(f"{trimmed}."):
                removed.append(mod_name)
                sys.modules.pop(mod_name, None)

        if removed:
            logging.debug("Cold reload purge for %s: %s", trimmed, removed)

    async def reload_cog(self, cog_name: str) -> Tuple[bool, str]:
        try:
            self._purge_namespace_modules(cog_name)
            await self.reload_extension(cog_name)
            self.cog_status[cog_name] = "loaded"
            await self.update_presence()
            msg = f"✅ Successfully reloaded {cog_name}"
            logging.info(msg)
            return True, msg
        except commands.ExtensionNotLoaded:
            try:
                self._purge_namespace_modules(cog_name)
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = "loaded"
                await self.update_presence()
                msg = f"✅ Loaded {cog_name} (was not loaded before)"
                logging.info(msg)
                return True, msg
            except Exception as e:
                err = f"❌ Failed to load {cog_name}: {str(e)[:200]}"
                self.cog_status[cog_name] = f"error: {str(e)[:100]}"
                logging.error(err)
                return False, err
        except Exception as e:
            err = f"❌ Failed to reload {cog_name}: {str(e)[:200]}"
            self.cog_status[cog_name] = f"error: {str(e)[:100]}"
            logging.error(err)
            return False, err

    async def reload_namespace(self, namespace: str) -> Dict[str, str]:
        try:
            target_ns = self.normalize_namespace(namespace)
        except ValueError:
            return {}

        if self.is_namespace_blocked(target_ns, assume_normalized=True):
            logging.info("Namespace %s ist blockiert – kein Reload", target_ns)
            return {}

        self.auto_discover_cogs()
        targets = [
            mod
            for mod in self.cogs_list
            if mod.startswith(target_ns) and not self._should_exclude(mod)
        ]

        if not targets:
            logging.info("Keine Cogs für Namespace %s gefunden", target_ns)
            return {}

        results: Dict[str, str] = {}
        for mod in targets:
            try:
                self._purge_namespace_modules(mod)
                if mod in self.extensions:
                    await self.reload_extension(mod)
                    results[mod] = "reloaded"
                    logging.info(f"🔁 Reloaded {mod}")
                else:
                    await self.load_extension(mod)
                    results[mod] = "loaded"
                    logging.info(f"✅ Loaded {mod}")
                self.cog_status[mod] = "loaded"
            except Exception as e:
                trimmed = str(e)[:200]
                results[mod] = f"error: {trimmed}"
                self.cog_status[mod] = f"error: {trimmed[:100]}"
                logging.error(f"❌ Reload error for {mod}: {e}")

        await self.update_presence()
        return results

    async def reload_steam_folder(self) -> Dict[str, str]:
        return await self.reload_namespace("cogs.steam")

    def _match_extensions(self, query: str) -> List[str]:
        q = query.strip().lower()
        loaded = [ext for ext in self.extensions.keys() if ext.startswith("cogs.")]
        if q.startswith("cogs."):
            return [ext for ext in loaded if ext.lower().startswith(q)]
        # erlaub Substring und Ordnerkurznamen
        return [
            ext
            for ext in loaded
            if q in ext.lower() or ext.lower().startswith(f"cogs.{q}.")
        ]

    async def unload_many(
        self, targets: List[str], timeout: float | None = None
    ) -> Dict[str, str]:
        timeout = float(timeout) if timeout is not None else self.per_cog_unload_timeout
        results: Dict[str, str] = {}
        for ext_name in targets:
            try:
                await asyncio.wait_for(self.unload_extension(ext_name), timeout=timeout)
                results[ext_name] = "unloaded"
                self.cog_status[ext_name] = "unloaded"
                logging.info(f"Unloaded extension: {ext_name}")
            except asyncio.TimeoutError:
                results[ext_name] = "timeout"
                logging.error(
                    f"Timeout unloading extension {ext_name} (>{timeout:.1f}s)"
                )
            except Exception as e:
                results[ext_name] = f"error: {str(e)[:200]}"
                logging.error(f"Error unloading extension {ext_name}: {e}")
        await self.update_presence()
        return results

from __future__ import annotations

import json
import argparse
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_auth_pool import main as pool


class RotationLogicTests(unittest.TestCase):
    def write_profile(
        self,
        root: Path,
        *,
        name: str,
        account_id: str,
        email: str,
        plan_type: str,
        disabled: bool = False,
    ) -> Path:
        path = root / f"{name}.json"
        pool.write_json(
            path,
            {
                "tokens": {
                    "access_token": f"a-{account_id}",
                    "refresh_token": f"r-{account_id}",
                    "id_token": "i",
                    "account_id": account_id,
                }
            },
        )
        pool.write_json(
            pool.meta_path_for_profile(path),
            {
                "email": email,
                "account_id": account_id,
                "observed_plan_type": plan_type,
                "disabled": disabled,
                "usage_checked_at": pool.now_local().isoformat(),
            },
        )
        return path

    def test_default_thresholds_are_exhaustion_only(self) -> None:
        now = pool.now_local()
        reason, until = pool.determine_rotation_trigger(
            None,
            primary_used_percent=99.4,
            primary_reset_at=now + timedelta(hours=5),
            secondary_used_percent=20.0,
            secondary_reset_at=now + timedelta(days=5),
            primary_threshold=pool.DEFAULT_PRIMARY_THRESHOLD,
            secondary_threshold=pool.DEFAULT_SECONDARY_THRESHOLD,
        )
        self.assertIsNone(reason)
        self.assertIsNone(until)

        reason, until = pool.determine_rotation_trigger(
            None,
            primary_used_percent=100.0,
            primary_reset_at=now + timedelta(hours=5),
            secondary_used_percent=20.0,
            secondary_reset_at=now + timedelta(days=5),
            primary_threshold=pool.DEFAULT_PRIMARY_THRESHOLD,
            secondary_threshold=pool.DEFAULT_SECONDARY_THRESHOLD,
        )
        self.assertEqual(reason, "primary_5h_limit")
        self.assertIsNotNone(until)

    def test_runtime_usage_flags_override_low_percentage(self) -> None:
        now = pool.now_local()
        usage = pool.RemoteUsageSnapshot(
            account_id="acct",
            email="user@example.com",
            plan_type="plus",
            allowed=False,
            limit_reached=False,
            primary_used_percent=1.0,
            primary_reset_at=now + timedelta(hours=5),
            primary_window_seconds=5 * 60 * 60,
            secondary_used_percent=10.0,
            secondary_reset_at=now + timedelta(days=5),
            secondary_window_seconds=7 * 24 * 60 * 60,
            fetched_at=now,
            source="test",
        )
        reason, until = pool.determine_rotation_trigger(
            usage,
            primary_used_percent=usage.primary_used_percent,
            primary_reset_at=usage.primary_reset_at,
            secondary_used_percent=usage.secondary_used_percent,
            secondary_reset_at=usage.secondary_reset_at,
            primary_threshold=pool.DEFAULT_PRIMARY_THRESHOLD,
            secondary_threshold=pool.DEFAULT_SECONDARY_THRESHOLD,
        )
        self.assertEqual(reason, "primary_5h_limit")
        self.assertEqual(until, usage.primary_reset_at)

    def test_rate_limits_null_signal_is_ignored_when_fresh_usage_is_allowed(self) -> None:
        now = pool.now_local()
        usage = pool.RemoteUsageSnapshot(
            account_id="acct",
            email="user@example.com",
            plan_type="prolite",
            allowed=True,
            limit_reached=False,
            primary_used_percent=11.0,
            primary_reset_at=now + timedelta(hours=4),
            primary_window_seconds=5 * 60 * 60,
            secondary_used_percent=2.0,
            secondary_reset_at=now + timedelta(days=7),
            secondary_window_seconds=7 * 24 * 60 * 60,
            fetched_at=now,
            source="wham_usage",
        )
        signal = pool.RuntimeLimitSignal(
            reason="primary_5h_limit",
            cooldown_until=None,
            primary_used_percent=None,
            secondary_used_percent=None,
            source_file=Path("rollout.jsonl"),
            event_timestamp=now.isoformat(),
            source="session_rate_limits_null",
            detail="consecutive token_count events had rate_limits=null",
        )

        self.assertTrue(
            pool.runtime_signal_is_overridden_by_fresh_usage(
                signal,
                usage,
                primary_threshold=pool.DEFAULT_PRIMARY_THRESHOLD,
                secondary_threshold=pool.DEFAULT_SECONDARY_THRESHOLD,
            )
        )

    def test_usage_limit_error_signal_is_not_ignored_by_fresh_usage(self) -> None:
        now = pool.now_local()
        usage = pool.RemoteUsageSnapshot(
            account_id="acct",
            email="user@example.com",
            plan_type="prolite",
            allowed=True,
            limit_reached=False,
            primary_used_percent=11.0,
            primary_reset_at=now + timedelta(hours=4),
            primary_window_seconds=5 * 60 * 60,
            secondary_used_percent=2.0,
            secondary_reset_at=now + timedelta(days=7),
            secondary_window_seconds=7 * 24 * 60 * 60,
            fetched_at=now,
            source="wham_usage",
        )
        signal = pool.RuntimeLimitSignal(
            reason="primary_5h_limit",
            cooldown_until=None,
            primary_used_percent=None,
            secondary_used_percent=None,
            source_file=Path("rollout.jsonl"),
            event_timestamp=now.isoformat(),
            source="session_error",
            detail="usage_limit_exceeded",
        )

        self.assertFalse(
            pool.runtime_signal_is_overridden_by_fresh_usage(
                signal,
                usage,
                primary_threshold=pool.DEFAULT_PRIMARY_THRESHOLD,
                secondary_threshold=pool.DEFAULT_SECONDARY_THRESHOLD,
            )
        )

    def test_recent_usage_refresh_failure_blocks_candidate_temporarily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            pool.write_json(
                path,
                {
                    "tokens": {
                        "access_token": "a",
                        "refresh_token": "r",
                        "id_token": "i",
                        "account_id": "acct",
                    }
                },
            )
            pool.write_json(
                pool.meta_path_for_profile(path),
                {
                    "usage_error": "temporary network failure",
                    "usage_error_checked_at": pool.now_local().isoformat(),
                },
            )
            until, reason = pool.observed_block_until_for_profile(path)
            self.assertEqual(reason, "usage_refresh_failed")
            self.assertIsNotNone(until)

    def test_browser_use_session_uses_longer_activity_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            captured_at = pool.now_local()
            active_at = captured_at - timedelta(seconds=pool.RECENT_SESSION_ACTIVITY_GRACE_SECONDS + 60)
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": active_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "Browser Use current url: https://example.com",
                        },
                    }
                )
            )
            session = pool.InterruptedSession(
                id="desktop-thread",
                title="Browser task",
                cwd=str(Path(tmp)),
                source="codex",
                model="gpt-5.5",
                rollout_path=str(rollout),
                updated_at=int(active_at.timestamp()),
                last_log_at=None,
                recent_log_count=1,
            )
            self.assertTrue(pool.session_was_in_progress_at(session, captured_at))

    def test_snapshot_selection_can_use_manifest_items_without_name_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "manual-good-snapshot"
            snapshot.mkdir()
            pool.write_json(
                pool.snapshot_manifest(snapshot),
                {
                    "items": [
                        {
                            "name": "app_support_browser_partition",
                            "source_path": "/tmp/source",
                            "stored_path": str(snapshot / "app_support_browser_partition"),
                        }
                    ]
                },
            )
            self.assertEqual(pool.select_browser_use_snapshot(root), snapshot)

    def test_goal_runtime_ignores_stale_blocker_after_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            now = pool.now_local()
            blocker_at = now - timedelta(minutes=5)
            progress_at = now - timedelta(minutes=4)
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": blocker_at.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "error", "message": "You've hit your usage limit"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": progress_at.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "agent_message", "message": "continued progress"},
                            }
                        ),
                    ]
                )
            )
            state = pool.classify_active_goal_runtime({"rollout_path": str(rollout)}, now=now)
            self.assertFalse(state["quota_blocked"])
            self.assertNotEqual(state["state"], "blocked")

    def test_goal_runtime_does_not_treat_research_text_as_quota_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            now = pool.now_local()
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "Blockers: automatic work has exhausted the selected route in the research plan.",
                        },
                    }
                )
            )
            state = pool.classify_active_goal_runtime({"rollout_path": str(rollout)}, now=now)
            self.assertFalse(state["quota_blocked"])

    def test_goal_runtime_detects_real_usage_limit_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            now = pool.now_local()
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "Error running remote compact task: You've hit your usage limit.",
                        },
                    }
                )
            )
            state = pool.classify_active_goal_runtime({"rollout_path": str(rollout)}, now=now)
            self.assertTrue(state["quota_blocked"])
            self.assertEqual(state["state"], "blocked")

    def test_goal_runtime_detects_stale_task_started_without_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            now = pool.now_local()
            started_at = now - timedelta(seconds=pool.ACTIVE_GOAL_STALE_SECONDS + 10)
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
            )
            state = pool.classify_active_goal_runtime({"rollout_path": str(rollout)}, now=now)
            self.assertEqual(state["state"], "interrupted")
            self.assertTrue(pool._runtime_state_requires_goal_resume(state))

    def test_goal_runtime_does_not_resume_after_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            now = pool.now_local()
            started_at = now - timedelta(seconds=pool.ACTIVE_GOAL_STALE_SECONDS + 20)
            completed_at = now - timedelta(seconds=pool.ACTIVE_GOAL_STALE_SECONDS + 10)
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": started_at.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": completed_at.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "task_complete"},
                            }
                        ),
                    ]
                )
            )
            state = pool.classify_active_goal_runtime({"rollout_path": str(rollout)}, now=now)
            self.assertEqual(state["state"], "stale")
            self.assertFalse(pool._runtime_state_requires_goal_resume(state))

    def test_runtime_limit_signal_can_exclude_active_goal_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            goal_rollout = sessions / "goal.jsonl"
            desktop_rollout = sessions / "desktop.jsonl"
            now = pool.now_local()
            goal_rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": now.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "token_count", "rate_limits": None},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": now.isoformat(),
                                "type": "event_msg",
                                "payload": {"type": "token_count", "rate_limits": None},
                            }
                        ),
                    ]
                )
            )
            desktop_rollout.write_text(
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {"used_percent": 20, "resets_at": now.timestamp()},
                                "secondary": {"used_percent": 5, "resets_at": now.timestamp()},
                            },
                        },
                    }
                )
            )

            signal = pool.latest_runtime_limit_signal(
                sessions,
                state={},
                max_age_minutes=30,
                exclude_rollout_paths={goal_rollout},
            )
            self.assertIsNone(signal)
            snapshot = pool.latest_rate_limit_snapshot(
                sessions,
                exclude_rollout_paths={goal_rollout},
            )
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_file, desktop_rollout)

    def test_large_rollout_scan_uses_tail_and_finds_recent_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            rollout = sessions / "large.jsonl"
            now = pool.now_local()
            with rollout.open("w", encoding="utf-8") as handle:
                handle.write("x" * (pool.DEFAULT_ROLLOUT_TAIL_BYTES + 1024))
                handle.write("\n")
                handle.write(
                    json.dumps(
                        {
                            "timestamp": now.isoformat(),
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "primary": {"used_percent": 12, "resets_at": now.timestamp()},
                                    "secondary": {"used_percent": 34, "resets_at": now.timestamp()},
                                },
                            },
                        }
                    )
                )
                handle.write("\n")

            lines = pool.read_tail_lines(rollout, max_bytes=2048)
            self.assertLess(sum(len(line) for line in lines), 4096)
            snapshot = pool.latest_rate_limit_snapshot(sessions)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.primary_used_percent, 12.0)

    def test_permanent_auth_failure_blocks_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile.json"
            pool.write_json(
                profile,
                {
                    "tokens": {
                        "access_token": "a",
                        "refresh_token": "r",
                        "id_token": "i",
                        "account_id": "acct",
                    }
                },
            )
            pool.write_json(
                pool.meta_path_for_profile(profile),
                {
                    "email": "bad@example.com",
                    "account_id": "acct",
                    "usage_error": "usage fetch failed with HTTP 401: Your authentication token has been invalidated. Please try signing in again.",
                    "usage_error_checked_at": pool.now_local().isoformat(),
                },
            )

            ranked = pool.rank_profiles(root / "missing-source", root, {}, root / "auth.json")
            self.assertEqual(len(ranked), 1)
            self.assertFalse(ranked[0]["available"])
            self.assertTrue(ranked[0]["permanent_auth_failure"])
            self.assertEqual(pool.profile_health(ranked[0]), "auth-invalidated")

    def test_app_policy_prefers_pro_before_plus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_profile(root, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(root, name="pro", account_id="pro-acct", email="pro@example.com", plan_type="pro")

            ranked = pool.rank_profiles(
                root / "missing-source",
                root,
                {},
                root / "auth.json",
                account_policy=pool.ACCOUNT_POLICY_APP,
            )
            available_emails = [item["summary"].email for item in ranked if item["available"]]
            self.assertEqual(available_emails, ["pro@example.com", "plus@example.com"])

    def test_app_policy_falls_back_to_plus_when_pro_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_profile(root, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(
                root,
                name="pro",
                account_id="pro-acct",
                email="pro@example.com",
                plan_type="pro",
                disabled=True,
            )

            ranked = pool.rank_profiles(
                root / "missing-source",
                root,
                {},
                root / "auth.json",
                account_policy=pool.ACCOUNT_POLICY_APP,
            )
            next_item = next(item for item in ranked if item["available"])
            self.assertEqual(next_item["summary"].email, "plus@example.com")

    def test_app_pro_promotion_candidate_when_current_is_plus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_profile(root, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(root, name="pro", account_id="pro-acct", email="pro@example.com", plan_type="pro")
            active_auth = root / "cache" / "auth.json"
            pool.write_json(
                active_auth,
                {
                    "tokens": {
                        "access_token": "a-plus",
                        "refresh_token": "r-plus",
                        "id_token": "i-plus",
                        "account_id": "plus-acct",
                    }
                },
            )

            candidate = pool.app_pro_promotion_candidate(
                root / "missing-source",
                root,
                {},
                active_auth,
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["summary"].email, "pro@example.com")

    def test_app_pro_promotion_skips_when_current_is_pro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_profile(root, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(root, name="pro", account_id="pro-acct", email="pro@example.com", plan_type="pro")
            active_auth = root / "cache" / "auth.json"
            pool.write_json(
                active_auth,
                {
                    "tokens": {
                        "access_token": "a-pro",
                        "refresh_token": "r-pro",
                        "id_token": "i-pro",
                        "account_id": "pro-acct",
                    }
                },
            )

            candidate = pool.app_pro_promotion_candidate(
                root / "missing-source",
                root,
                {},
                active_auth,
            )
            self.assertIsNone(candidate)

    def test_cli_policy_allows_plus_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_profile(root, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(root, name="pro", account_id="pro-acct", email="pro@example.com", plan_type="pro")

            ranked = pool.rank_profiles(
                root / "missing-source",
                root,
                {},
                root / "auth.json",
                account_policy=pool.ACCOUNT_POLICY_CLI,
            )
            plus = next(item for item in ranked if item["summary"].account_id == "plus-acct")
            pro = next(item for item in ranked if item["summary"].account_id == "pro-acct")
            self.assertTrue(plus["available"])
            self.assertFalse(pro["available"])
            self.assertEqual(pool.profile_health(pro), "policy-excluded-pro")

            allowed, tier, reason = pool.account_allows_cli_goal_resume(root / "missing-source", root, "pro-acct")
            self.assertFalse(allowed)
            self.assertEqual(tier, "pro")
            self.assertEqual(reason, "cli_goal_resume_requires_plus")

    def test_account_summary_prefers_managed_plan_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cliproxy"
            managed = root / "managed"
            source.mkdir()
            managed.mkdir()
            self.write_profile(source, name="codex-plus", account_id="acct", email="plus@example.com", plan_type="")
            self.write_profile(managed, name="codex-plus", account_id="acct", email="plus@example.com", plan_type="plus")

            summary = pool.account_summary_by_id(source, managed, "acct")
            self.assertIsNotNone(summary)
            self.assertEqual(summary.plan_type, "plus")
            allowed, tier, reason = pool.account_allows_cli_goal_resume(source, managed, "acct")
            self.assertTrue(allowed)
            self.assertEqual(tier, "plus")
            self.assertEqual(reason, "plus")

    def test_cli_plus_home_is_isolated_and_selects_plus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            self.write_profile(managed, name="plus", account_id="plus-acct", email="plus@example.com", plan_type="plus")
            self.write_profile(managed, name="pro", account_id="pro-acct", email="pro@example.com", plan_type="pro")

            global_home = root / "global-codex"
            (global_home / "cache").mkdir(parents=True)
            (global_home / "sessions").mkdir()
            (global_home / "config.toml").write_text("model = \"gpt-5.5\"\n")
            global_auth = {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "global-a",
                    "refresh_token": "global-r",
                    "id_token": "global-i",
                    "account_id": "pro-acct",
                },
            }
            pool.write_json(global_home / "cache" / "auth.json", global_auth)

            args = argparse.Namespace(
                source_dir=root / "missing-source",
                managed_dir=managed,
                events_path=root / "events.jsonl",
                state_path=root / "state.json",
                target=str(global_home / "cache" / "auth.json"),
                usage_max_age_minutes=pool.DEFAULT_USAGE_MAX_AGE_MINUTES,
                skip_usage_validation=True,
                cli_plus_home=root / "cli-plus-home",
                source_codex_home=global_home,
            )

            _, summary, cli_home, auth_paths, linked = pool.prepare_cli_plus_home(args)

            self.assertEqual(summary.email, "plus@example.com")
            self.assertEqual(pool.read_json(global_home / "cache" / "auth.json"), global_auth)
            self.assertTrue((cli_home / "config.toml").is_symlink())
            self.assertTrue((cli_home / "sessions").is_symlink())
            self.assertIn("config.toml", linked)
            self.assertIn("sessions", linked)
            for auth_path in auth_paths:
                payload = pool.read_json(auth_path)
                self.assertEqual(payload["tokens"]["account_id"], "plus-acct")
                self.assertEqual(payload["auth_mode"], "chatgpt")
            self.assertEqual(pool.cli_plus_active_account_id(cli_home), "plus-acct")

    def test_cli_plus_home_rotates_when_active_plus_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            exhausted = self.write_profile(
                managed,
                name="old-plus",
                account_id="old-plus",
                email="old@example.com",
                plan_type="plus",
            )
            available = self.write_profile(
                managed,
                name="new-plus",
                account_id="new-plus",
                email="new@example.com",
                plan_type="plus",
            )
            old_meta = pool.read_profile_metadata(exhausted)
            old_meta.update(
                {
                    "observed_allowed": False,
                    "observed_limit_reached": True,
                    "observed_primary_used_percent": 100.0,
                    "observed_primary_reset_at": (pool.now_local() + timedelta(hours=2)).isoformat(),
                }
            )
            pool.write_json(pool.meta_path_for_profile(exhausted), old_meta)
            new_meta = pool.read_profile_metadata(available)
            new_meta.update(
                {
                    "observed_allowed": True,
                    "observed_limit_reached": False,
                    "observed_primary_used_percent": 10.0,
                    "observed_primary_reset_at": (pool.now_local() + timedelta(hours=2)).isoformat(),
                }
            )
            pool.write_json(pool.meta_path_for_profile(available), new_meta)

            cli_home = root / "cli-plus-home"
            auth_payload = pool.convert_profile(pool.normalize_source_payload(exhausted, pool.read_json(exhausted)))
            for auth_path in pool.cli_plus_auth_paths(cli_home):
                pool.write_json(auth_path, auth_payload)

            args = argparse.Namespace(
                source_dir=root / "missing-source",
                managed_dir=managed,
                events_path=root / "events.jsonl",
                state_path=root / "state.json",
                target=str(root / "global" / "cache" / "auth.json"),
                usage_max_age_minutes=pool.DEFAULT_USAGE_MAX_AGE_MINUTES,
                skip_usage_validation=True,
                refresh_usage=False,
                cli_plus_home=cli_home,
                source_codex_home=root / "global",
                daemon_quiet=True,
            )

            result = pool.rotate_cli_plus_home_if_needed(args)

            self.assertIsNotNone(result)
            self.assertEqual(result["old_account_id"], "old-plus")
            self.assertEqual(result["new_account_id"], "new-plus")
            self.assertEqual(pool.cli_plus_active_account_id(cli_home), "new-plus")

    def test_install_cli_wrapper_writes_codex_plus_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = Path(tmp) / "bin" / "codex-plus"
            result = pool.cmd_install_cli_wrapper(argparse.Namespace(bin_path=bin_path))
            self.assertEqual(result, 0)
            self.assertTrue(bin_path.exists())
            self.assertTrue(bin_path.stat().st_mode & 0o111)
            self.assertIn("codex-auth-pool", bin_path.read_text())

    def test_goal_resume_command_prefers_pending_record(self) -> None:
        goal = {"thread_id": "thread-1"}
        state = {
            "pending_goal_resumes": {
                "thread-1": {
                    "thread_id": "thread-1",
                    "resume_command": "codex-plus",
                }
            }
        }
        self.assertEqual(pool.goal_resume_command(goal, state, default="codex"), "codex-plus")

    def test_goal_resume_command_defaults_to_codex(self) -> None:
        self.assertEqual(pool.goal_resume_command({"thread_id": "thread-1"}, {}, default="codex"), "codex")

    def test_cli_plus_resume_invocation_records_original_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_id = "019fffffffffffffffffffffffffffffffff"
            state_path = root / "state.json"
            events_path = root / "events.jsonl"
            pool.write_json(
                state_path,
                {
                    "pending_goal_resumes": {
                        thread_id: {
                            "thread_id": thread_id,
                            "goal_id": "goal-1",
                            "title": "Goal title",
                            "cwd": str(root),
                            "resume_command": "codex",
                        }
                    }
                },
            )
            args = argparse.Namespace(state_path=state_path, events_path=events_path)
            summary = pool.ProfileSummary(
                path=root / "plus.json",
                source_kind="managed",
                email="plus@example.com",
                account_id="plus-acct",
                plan_type="plus",
                weekly_reset_at=None,
                last_refresh=None,
                expired=None,
                disabled=False,
            )

            pool._record_cli_plus_resume_invocation(args, summary, ["resume", thread_id, "继续"])

            state = pool.read_json(state_path)
            self.assertEqual(state["pending_goal_resumes"][thread_id]["resume_command"], "codex-plus")
            self.assertEqual(
                pool.goal_resume_command({"thread_id": thread_id}, state, default="codex"),
                "codex-plus",
            )

    def test_goal_discovery_for_resume_does_not_include_paused_goals(self) -> None:
        calls: list[tuple[str, ...] | None] = []
        original_read_active = pool.read_active_goal_threads
        original_read_by_ids = pool.read_goal_threads_by_ids
        original_running_ids = pool._running_codex_resume_thread_ids
        try:
            def fake_read_active_goal_threads(**kwargs):
                calls.append(kwargs.get("statuses"))
                return []

            pool.read_active_goal_threads = fake_read_active_goal_threads  # type: ignore[assignment]
            pool.read_goal_threads_by_ids = lambda **kwargs: {}  # type: ignore[assignment]
            pool._running_codex_resume_thread_ids = lambda: set()  # type: ignore[assignment]
            pool.discover_goal_threads_for_resume(
                codex_state_db=Path("state.sqlite3"),
                state={},
                max_count=5,
            )
        finally:
            pool.read_active_goal_threads = original_read_active  # type: ignore[assignment]
            pool.read_goal_threads_by_ids = original_read_by_ids  # type: ignore[assignment]
            pool._running_codex_resume_thread_ids = original_running_ids  # type: ignore[assignment]

        self.assertEqual(calls, [("active",)])


if __name__ == "__main__":
    unittest.main()

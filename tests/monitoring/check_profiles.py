"""Run dashboard SQL and read-only grants against a disposable local PostgreSQL.

Requires PyYAML and PostgreSQL 16+ binaries on PATH. No cluster access needed.
Run: python3 tests/monitoring/check_profiles.py
"""

import json
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "infrastructure/base/controllers/monitoring/dashboards/dashboard-profiles-devices.yaml"
PROFILE = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
OLDER = "00000000-0000-0000-0000-000000000003"


def walk_panels(panels):
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def main():
    assert DASHBOARD.exists(), "Profiles dashboard is missing"
    dashboard = json.loads(next(iter(yaml.safe_load(DASHBOARD.read_text())["data"].values())))
    with tempfile.TemporaryDirectory(prefix="fin-profiles-") as directory:
        subprocess.run(["initdb", "-D", f"{directory}/db", "-A", "trust", "-U", "postgres"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["pg_ctl", "-D", f"{directory}/db", "-l", f"{directory}/log", "-o",
                        f"-k {directory} -p 55489 -c listen_addresses=''", "-w", "start"],
                       check=True, stdout=subprocess.DEVNULL)
        command = ["psql", "-X", "-h", directory, "-p", "55489", "-U", "postgres", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", "-At"]

        def sql(query):
            result = subprocess.run(command, input=query, text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        def rows(query, device="device'one"):
            query = query.replace("${device_id:sqlstring}", "'" + device.replace("'", "''") + "'")
            return json.loads(sql(f"SELECT coalesce(json_agg(t), '[]') FROM ({query.rstrip(';')}) t"))

        try:
            sql(f"""
                CREATE ROLE grafana_reader LOGIN;
                CREATE ROLE app_owner LOGIN;
                ALTER DATABASE postgres OWNER TO app_owner;
                SET ROLE app_owner;
                CREATE TABLE profiles(profile_id uuid PRIMARY KEY, device_id text UNIQUE, pet_json json,
                    last_synced_at timestamptz);
                CREATE TABLE snapshot_heads(profile_id uuid PRIMARY KEY, game_run_id text,
                    server_revision bigint, snapshot_json text);
                CREATE TABLE analytics_heads(profile_id uuid, game_run_id text, through_history_sequence bigint);
                CREATE TABLE analytics_original_facts(profile_id uuid, game_run_id text, event_id text, sequence bigint);
                CREATE TABLE analytics_projections(profile_id uuid, game_run_id text, projection_version int,
                    evaluator_version int, through_history_sequence bigint, history_start_sequence bigint,
                    revision bigint, facts jsonb, skills jsonb);
                CREATE TABLE skill_assessments(profile_id uuid, game_run_id text, policy_version text,
                    based_on_history_sequence bigint, skills jsonb);
                CREATE TABLE parent_rewards(reward_id uuid, profile_id uuid, game_run_id text,
                    sequence bigint, reward_type text, amount bigint, item_id text, created_at timestamptz);
                CREATE TABLE parent_reward_receipts(reward_id uuid, outcome text);
                INSERT INTO profiles(profile_id, device_id, pet_json) VALUES
                    ('{PROFILE}', 'device''one', '{{"name":"Мурзик","age":"YOUNG"}}'),
                    ('{OTHER}', 'empty-device', '{{"name":"Другой"}}'),
                    ('{OLDER}', 'older-device', '{{"name":"Старый"}}');
                UPDATE profiles SET last_synced_at='2026-10-20T10:00:00Z' WHERE profile_id='{PROFILE}';
                UPDATE profiles SET last_synced_at='2026-10-19T10:00:00Z' WHERE profile_id='{OLDER}';
                INSERT INTO snapshot_heads VALUES
                    ('{PROFILE}', 'current', 3, '{{"state":{{"economy":{{"availableBalance":42,"savingsBalance":10}},"engine":{{"day":7}}}}}}'),
                    ('{OTHER}', 'empty', 0, '');
                INSERT INTO analytics_original_facts VALUES
                    ('{PROFILE}', 'current', 'event-1', 1), ('{PROFILE}', 'old', 'event-1', 1);
                INSERT INTO analytics_heads VALUES ('{PROFILE}', 'current', 2), ('{PROFILE}', 'old', 1);
                INSERT INTO analytics_projections VALUES
                    ('{PROFILE}', 'current', 4, 1, 1, 0, 0,
                     '[{{"eventId":"e1","sequence":1,"detail":{{"_type":"interaction"}}}}]', '[]'),
                    ('{PROFILE}', 'current', 4, 1, 2, 0, 0,
                     '[{{"eventId":"stale","sequence":1}}]', '[]'),
                    ('{PROFILE}', 'current', 4, 1, 2, 0, 1,
                     '[{{"eventId":"e1","sequence":1,"detail":{{"_type":"interaction"}}}},
                       {{"eventId":"e2","sequence":2,"detail":{{"_type":"practice_answer","correct":true}}}}]', '[]'),
                    ('{PROFILE}', 'old', 4, 1, 1, 0, 0, '[{{"eventId":"old","sequence":1}}]', '[]');
                INSERT INTO skill_assessments VALUES ('{PROFILE}', 'current', 'v1', 2,
                    '[{{"skillId":"FIN-01","status":"SUPPORTED","observationCounts":{{"completedEpisodes":2}}}}]');
                INSERT INTO parent_rewards VALUES
                    ('{OTHER}', '{PROFILE}', 'current', 1, 'COINS', 20, NULL, '2026-10-20T00:00:00Z');
                INSERT INTO parent_reward_receipts VALUES ('{OTHER}', 'APPLIED'), ('{OTHER}', 'APPLIED');
                CREATE TABLE private_data(token text);
            """)
            grants = yaml.safe_load((ROOT / "apps/grafana-postgres/grants.yaml").read_text())
            script = grants["spec"]["template"]["spec"]["containers"][0]["args"][0]
            grant_sql = script.split("<<'SQL'\n", 1)[1].rsplit("\nSQL", 1)[0]
            sql("SET ROLE app_owner; " + grant_sql.replace(':"reader"', 'grafana_reader').replace(':"database"', 'postgres'))
            assert sql("SELECT has_table_privilege('grafana_reader','profiles','SELECT')") == "t"
            assert sql("SELECT has_table_privilege('grafana_reader','profiles','UPDATE')") == "f"
            assert sql("SELECT has_table_privilege('grafana_reader','private_data','SELECT')") == "f"
            variable = dashboard["templating"]["list"][0]
            choices = rows(variable["query"])
            assert len(choices) == 3
            assert next(c for c in choices if c["__value"] == "device'one")["__text"] == "Мурзик · device'one"
            panels = {p["id"]: p for p in walk_panels(dashboard["panels"]) if p.get("targets")}
            results = {key: rows(panel["targets"][0]["rawSql"]) for key, panel in panels.items()}
            assert len(results[10]) == 3, "Overview must include profiles without analytics"
            assert [r['device_id'] for r in results[10]] == ["device'one", 'older-device', 'empty-device']
            assert results[10][-1]['Последняя синхронизация'] is None
            first = next(r for r in results[10] if r["device_id"] == "device'one")
            assert first["Исходные события"] == 2, first
            assert first["Награды"] == 1, "Receipt joins must not multiply rewards"
            assert first["Оценки навыков"] == "v1 · FIN-01: SUPPORTED", first
            assert len(results[20]) == 1 and results[20][0]["Баланс"] == 42
            assert len(results[30]) == 1 and results[30][0]["Навык"] == "FIN-01"
            assert len(results[40]) == 2, "Use latest projection, exclude old runs and revisions"
            assert len(results[50]) == 1
            assert results[21] == [{"Питомец": "Мурзик"}]
            assert results[23] == [{"Баланс": 42}]
            assert results[24] == [{"Накопления": 10}]
            assert rows(panels[23]["targets"][0]["rawSql"], "empty-device") == [{"Баланс": None}]
            assert rows(panels[23]["targets"][0]["rawSql"], "unknown") == []
            for key in (30, 40, 50):
                assert rows(panels[key]["targets"][0]["rawSql"], "empty-device") == []
                assert rows(panels[key]["targets"][0]["rawSql"], "unknown") == []
            empty = rows(panels[20]["targets"][0]["rawSql"], "empty-device")
            assert empty[0]["Баланс"] is None, "Missing snapshot must not become zero"
            sql(f"UPDATE snapshot_heads SET snapshot_json='broken json' WHERE profile_id='{OTHER}'")
            rows(panels[20]["targets"][0]["rawSql"], "empty-device")
            sql(f"UPDATE snapshot_heads SET snapshot_json="
                "'{\"state\":{\"economy\":{\"availableBalance\":true}}}' "
                f"WHERE profile_id='{OTHER}'")
            malformed = rows(panels[20]["targets"][0]["rawSql"], "empty-device")
            assert malformed[0]["Баланс"] is None, "Invalid money must not break the dashboard"
            for panel in panels.values():
                query = panel["targets"][0]["rawSql"].replace("${device_id:sqlstring}", "'device''one'")
                sql("SET ROLE grafana_reader; " + query)
            sql("ALTER TABLE profiles DROP COLUMN last_synced_at")
            old_schema = rows(panels[10]['targets'][0]['rawSql'])
            assert all(r['Последняя синхронизация'] is None for r in old_schema), "Dashboard must work before backend migration"
            print(f"Verified {len(panels)} panel queries, device isolation, empty/invalid snapshots and read-only grants.")
        finally:
            subprocess.run(["pg_ctl", "-D", f"{directory}/db", "-m", "immediate", "-w", "stop"],
                           check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()

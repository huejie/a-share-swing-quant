from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

SCHEMA_VERSION = 2


class SQLiteRepository:
    """Small append-oriented product store; stdlib-only and safe across restarts."""

    def __init__(self, path: str | Path = "data/quant_system.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._migrate()

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _migrate(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS decisions(
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data_timestamp TEXT NOT NULL,
              model_version TEXT NOT NULL, run_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backtests(
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
              summary TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_keys(
              run_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
              response TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS input_snapshots(
              content_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, provider TEXT NOT NULL,
              data_timestamp TEXT NOT NULL, encoding TEXT NOT NULL, payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulation_ledger(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL, event_time TEXT NOT NULL,
              effective_at TEXT NOT NULL, event_type TEXT NOT NULL, symbol TEXT,
              quantity INTEGER, price REAL, amount REAL, fee REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL, payload TEXT NOT NULL,
              UNIQUE(run_key, event_type, symbol)
            );
            CREATE TABLE IF NOT EXISTS simulation_equity(
              id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
              trade_date TEXT NOT NULL, recorded_at TEXT NOT NULL,
              model_version TEXT NOT NULL, provider TEXT NOT NULL,
              config_hash TEXT NOT NULL, settings_version INTEGER NOT NULL,
              data_snapshot_hash TEXT NOT NULL,
              cash REAL NOT NULL, market_value REAL NOT NULL, equity REAL NOT NULL,
              drawdown REAL NOT NULL, payload TEXT NOT NULL,
              UNIQUE(account_id,trade_date,model_version,provider,config_hash,
                     settings_version,data_snapshot_hash)
            );
            CREATE TABLE IF NOT EXISTS simulation_accounts(
              id TEXT PRIMARY KEY, initial_capital REAL NOT NULL, cash REAL NOT NULL,
              peak_equity REAL NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulation_positions(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, shares INTEGER NOT NULL,
              avg_cost REAL NOT NULL, updated_at TEXT NOT NULL,
              last_price REAL, last_price_at TEXT,
              PRIMARY KEY(account_id,symbol),
              FOREIGN KEY(account_id) REFERENCES simulation_accounts(id)
            );
            CREATE TABLE IF NOT EXISTS simulation_corporate_actions(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, effective_date TEXT NOT NULL,
              applied_at TEXT NOT NULL, share_multiplier REAL NOT NULL,
              cash_dividend_per_share REAL NOT NULL, old_shares INTEGER NOT NULL,
              new_shares INTEGER NOT NULL, cash_delta REAL NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(account_id,symbol,effective_date)
            );
            CREATE TABLE IF NOT EXISTS settings_profiles(
              id TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings_audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL,
              version INTEGER NOT NULL, changed_at TEXT NOT NULL, actor TEXT NOT NULL,
              previous_payload TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_outbox(
              id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              event_type TEXT NOT NULL, channel TEXT NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              payload TEXT NOT NULL, last_error TEXT, sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notification_outbox_created
              ON notification_outbox(created_at DESC);
            CREATE TABLE IF NOT EXISTS schema_meta(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              version INTEGER NOT NULL, migrated_at TEXT NOT NULL
            );
            """)
            self._migrate_simulation_schema(db)
            db.execute(
                "INSERT INTO schema_meta(singleton,version,migrated_at) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,migrated_at=excluded.migrated_at",
                (SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )

    @staticmethod
    def _migrate_simulation_schema(db: sqlite3.Connection) -> None:
        """Upgrade pre-lineage paper accounts without overwriting their history."""
        equity_columns = {row["name"] for row in db.execute("PRAGMA table_info(simulation_equity)")}
        if "id" not in equity_columns:
            db.execute("ALTER TABLE simulation_equity RENAME TO simulation_equity_legacy")
            db.executescript("""
            CREATE TABLE simulation_equity(
              id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
              trade_date TEXT NOT NULL, recorded_at TEXT NOT NULL,
              model_version TEXT NOT NULL, provider TEXT NOT NULL,
              config_hash TEXT NOT NULL, settings_version INTEGER NOT NULL,
              data_snapshot_hash TEXT NOT NULL,
              cash REAL NOT NULL, market_value REAL NOT NULL, equity REAL NOT NULL,
              drawdown REAL NOT NULL, payload TEXT NOT NULL,
              UNIQUE(account_id,trade_date,model_version,provider,config_hash,
                     settings_version,data_snapshot_hash)
            );
            """)
            # Legacy rows are deliberately retained in a separate audit table:
            # they lack immutable lineage fields and can never qualify as a
            # frozen-model forward observation.
        position_columns = {row["name"] for row in db.execute("PRAGMA table_info(simulation_positions)")}
        if "last_price" not in position_columns:
            db.execute("ALTER TABLE simulation_positions ADD COLUMN last_price REAL")
        if "last_price_at" not in position_columns:
            db.execute("ALTER TABLE simulation_positions ADD COLUMN last_price_at TEXT")

    def ping(self) -> bool:
        try:
            with self.connect() as db: return db.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error: return False

    def schema_version(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT version FROM schema_meta WHERE singleton=1").fetchone()
        return int(row[0]) if row else 0

    def get_run(self, run_key: str) -> dict | None:
        with self.connect() as db: row=db.execute("SELECT response FROM run_keys WHERE run_key=?",(run_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, run_key: str, status: str, response: dict):
        encoded=json.dumps(response,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        with self._lock,self.connect() as db:
            db.execute("INSERT OR IGNORE INTO run_keys VALUES(?,?,?,?)",(run_key,datetime.now().astimezone().isoformat(),status,encoded))

    def save_decision(self, decision: dict, run_key: str):
        encoded=json.dumps(decision,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        with self._lock,self.connect() as db:
            db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?)",(decision["id"],decision["timestamp"],decision["data_timestamp"],decision["model_version"],run_key,encoded))

    def save_input_snapshot(self, content_hash:str, snapshot:dict):
        encoded=json.dumps(snapshot,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode()
        payload=zlib.compress(encoded,level=9)
        with self._lock,self.connect() as db:
            db.execute("INSERT OR IGNORE INTO input_snapshots VALUES(?,?,?,?,?,?)",(
                content_hash,datetime.now().astimezone().isoformat(),str(snapshot.get("provider","unknown")),
                str(snapshot.get("as_of","")),"zlib-json-v1",payload,
            ))

    def get_input_snapshot(self,content_hash:str)->dict|None:
        with self.connect() as db:
            row=db.execute("SELECT encoding,payload FROM input_snapshots WHERE content_hash=?",(content_hash,)).fetchone()
        if row is None:return None
        if row["encoding"]!="zlib-json-v1":raise ValueError("unsupported input snapshot encoding")
        return json.loads(zlib.decompress(row["payload"]).decode())

    def input_snapshot_status(self,content_hash:str)->dict:
        with self.connect() as db:
            row=db.execute("SELECT provider,data_timestamp,encoding,length(payload) AS compressed_bytes FROM input_snapshots WHERE content_hash=?",(content_hash,)).fetchone()
        return ({"available":False,"content_hash":content_hash} if row is None else
                {"available":True,"content_hash":content_hash,**dict(row)})

    def list_decisions(self, limit=20) -> list[dict]:
        with self.connect() as db: rows=db.execute("SELECT payload FROM decisions ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
        return [json.loads(x[0]) for x in rows]

    def get_decision(self, ident: str) -> dict | None:
        with self.connect() as db: row=db.execute("SELECT payload FROM decisions WHERE id=?",(ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def load_settings(self, profile_id: str = "personal") -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT version,updated_at,payload FROM settings_profiles WHERE id=?",
                             (profile_id,)).fetchone()
        if row is None:
            return None
        return {**json.loads(row["payload"]), "settings_version": int(row["version"]),
                "settings_updated_at": row["updated_at"]}

    def save_settings(self, payload: dict, *, profile_id: str = "personal", actor: str = "product-api") -> dict:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        now = datetime.now().astimezone().isoformat()
        with self._lock, self.connect() as db:
            row = db.execute("SELECT version,payload FROM settings_profiles WHERE id=?", (profile_id,)).fetchone()
            previous = row["payload"] if row else "{}"
            version = int(row["version"]) + 1 if row else 1
            db.execute(
                "INSERT INTO settings_profiles VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at,payload=excluded.payload",
                (profile_id, version, now, encoded),
            )
            db.execute("INSERT INTO settings_audit(profile_id,version,changed_at,actor,previous_payload,payload) VALUES(?,?,?,?,?,?)",
                       (profile_id, version, now, actor, previous, encoded))
        return {**payload, "settings_version": version, "settings_updated_at": now}

    def list_settings_audit(self, profile_id: str = "personal", limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT version,changed_at,actor,previous_payload,payload FROM settings_audit "
                "WHERE profile_id=? ORDER BY id DESC LIMIT ?", (profile_id, limit),
            ).fetchall()
        return [{"version": int(row["version"]), "changed_at": row["changed_at"], "actor": row["actor"],
                 "previous": json.loads(row["previous_payload"]), "settings": json.loads(row["payload"])}
                for row in rows]

    @staticmethod
    def _notification_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["attempts"] = int(item["attempts"])
        return item

    def enqueue_notification(self, ident: str, event_key: str, event_type: str,
                             channel: str, payload: dict) -> dict:
        """Persist a secret-free event once; ``event_key`` is the idempotency boundary."""
        now = datetime.now().astimezone().isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO notification_outbox"
                "(id,event_key,created_at,updated_at,event_type,channel,status,attempts,payload,last_error,sent_at) "
                "VALUES(?,?,?,?,?,?,?,0,?,NULL,NULL)",
                (ident, event_key, now, now, event_type, channel, "pending", encoded),
            )
            row = db.execute("SELECT * FROM notification_outbox WHERE event_key=?", (event_key,)).fetchone()
        return self._notification_row(row)

    def finish_notification(self, ident: str, status: str, *, error: str | None = None) -> dict | None:
        if status not in {"sent", "failed", "skipped"}:
            raise ValueError("unsupported notification status")
        now = datetime.now().astimezone().isoformat()
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE notification_outbox SET status=?,updated_at=?,"
                "last_error=?,sent_at=? WHERE id=?",
                (status, now, error, now if status == "sent" else None, ident),
            )
            row = db.execute("SELECT * FROM notification_outbox WHERE id=?", (ident,)).fetchone()
        return self._notification_row(row)

    def claim_notification(self, ident: str, from_status: str) -> dict | None:
        """Atomically claim one delivery attempt across threads/processes."""
        now = datetime.now().astimezone().isoformat()
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "UPDATE notification_outbox SET status='sending',attempts=attempts+1,"
                "updated_at=?,last_error=NULL WHERE id=? AND status=?",
                (now, ident, from_status),
            )
            row = db.execute("SELECT * FROM notification_outbox WHERE id=?", (ident,)).fetchone()
        return self._notification_row(row) if cursor.rowcount else None

    def get_notification(self, ident: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM notification_outbox WHERE id=?", (ident,)).fetchone()
        return self._notification_row(row)

    def list_notifications(self, limit: int = 50, *, status: str | None = None,
                           event_type: str | None = None) -> list[dict]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if event_type:
            clauses.append("event_type=?")
            values.append(event_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM notification_outbox{where} ORDER BY created_at DESC LIMIT ?", values
            ).fetchall()
        return [self._notification_row(row) for row in rows]

    def save_backtest(self, ident: str, result: dict):
        summary={k:v for k,v in result.items() if k not in ("fills","equity_curve")}
        now=datetime.now().astimezone().isoformat()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO backtests VALUES(?,?,?,?,?)",(ident,now,result["status"],json.dumps(summary,ensure_ascii=False),json.dumps(result,ensure_ascii=False)))

    def list_backtests(self) -> list[dict]:
        with self.connect() as db: rows=db.execute("SELECT summary FROM backtests ORDER BY created_at DESC").fetchall()
        return [json.loads(x[0]) for x in rows]

    def get_backtest(self, ident: str) -> dict | None:
        with self.connect() as db: row=db.execute("SELECT payload FROM backtests WHERE id=?",(ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def ensure_simulation_account(self, capital: float, account_id: str="model") -> dict:
        now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            db.execute("INSERT OR IGNORE INTO simulation_accounts VALUES(?,?,?,?,?)",(account_id,capital,capital,capital,now))
            row=db.execute("SELECT * FROM simulation_accounts WHERE id=?",(account_id,)).fetchone()
        return dict(row)

    def reconfigure_simulation_account(self, capital: float, account_id: str = "model") -> dict:
        """Start a clean capital lineage only while no virtual fill/position exists.

        A capital edit must not silently rescale an already executed paper
        portfolio.  Before the first fill we can safely cancel pending intents
        and reset the empty account; afterwards the caller must create a new
        simulation lineage instead.
        """
        now = datetime.now().astimezone().isoformat()
        with self._lock, self.connect() as db:
            positions = db.execute("SELECT COUNT(*) FROM simulation_positions WHERE account_id=?",
                                   (account_id,)).fetchone()[0]
            fills = db.execute("SELECT COUNT(*) FROM simulation_ledger WHERE status IN ('filled','partial')").fetchone()[0]
            if positions or fills:
                raise ValueError("模拟账户已有成交或持仓，不能原地修改本金；请保留历史并新建模拟账户")
            db.execute("UPDATE simulation_ledger SET status='cancelled' WHERE status='pending'")
            # Immutable equity observations remain audit evidence. The new
            # capital/config hash starts a separate qualifying lineage.
            db.execute(
                "INSERT INTO simulation_accounts VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET initial_capital=excluded.initial_capital,cash=excluded.cash,"
                "peak_equity=excluded.peak_equity,updated_at=excluded.updated_at",
                (account_id, capital, capital, capital, now),
            )
            row = db.execute("SELECT * FROM simulation_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row)

    def simulation_positions(self, account_id: str="model") -> dict[str,dict]:
        with self.connect() as db:rows=db.execute("SELECT * FROM simulation_positions WHERE account_id=?",(account_id,)).fetchall()
        return {x["symbol"]:dict(x) for x in rows}

    def simulation_last_trade_date(self, account_id: str="model") -> str | None:
        with self.connect() as db:
            row=db.execute("SELECT MAX(trade_date) AS day FROM simulation_equity WHERE account_id=?",
                           (account_id,)).fetchone()
            if row and row["day"]:return str(row["day"])
            legacy_exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulation_equity_legacy'").fetchone()
            if legacy_exists:
                legacy=db.execute("SELECT MAX(trade_date) AS day FROM simulation_equity_legacy").fetchone()
                if legacy and legacy["day"]:return str(legacy["day"])
        return None

    def append_simulation_intents(self, run_key: str, created_at: str, effective_at: str, intents: list[dict]):
        with self._lock,self.connect() as db:
            for p in intents:
                side=p["side"]
                payload={"label":"模拟意图","broker_connected":False,"side":side,"target_weight":p.get("target_weight",0),
                         "initial_weight":p.get("initial_weight",0),"requested_quantity":p.get("quantity"),
                         "current_weight":p.get("current_weight",0),
                         "execution_target_weight":p.get("execution_target_weight",p.get("target_weight",0)),
                         "model_action":p.get("model_action"),"stage":p.get("stage")}
                db.execute("INSERT OR IGNORE INTO simulation_ledger(run_key,event_time,effective_at,event_type,symbol,quantity,price,amount,fee,status,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_key,created_at,effective_at,f"intent_{side}",p["symbol"],p.get("quantity"),None,p.get("amount"),0,"pending",json.dumps(payload,ensure_ascii=False)))

    def replace_simulation_intents(self, run_key: str, created_at: str, effective_at: str,
                                   intents: list[dict]) -> None:
        """Supersede stale unfilled targets before appending one coherent model plan."""
        now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            pending=db.execute("SELECT id,payload FROM simulation_ledger WHERE status='pending'").fetchall()
            for row in pending:
                payload=json.loads(row["payload"])
                payload.update({"cancel_reason":"superseded_by_new_model_plan","superseded_by":run_key})
                db.execute("UPDATE simulation_ledger SET status='cancelled',event_time=?,payload=? WHERE id=?",
                           (now,json.dumps(payload,ensure_ascii=False),row["id"]))
        self.append_simulation_intents(run_key,created_at,effective_at,intents)

    def apply_corporate_actions(self, run_key: str, bars: dict[str,Any] | list[Any],
                                account_id: str="model") -> list[dict]:
        """Apply split/bonus-share and cash-dividend events exactly once.

        ``adj_factor`` is intentionally ignored here: it makes price signals
        continuous but is not, by itself, evidence of an account cash/share
        movement.  Only the licensed event fields affect the paper ledger.
        """
        applied=[];now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            account=db.execute("SELECT cash FROM simulation_accounts WHERE id=?",(account_id,)).fetchone()
            if account is None:return []
            cash=float(account["cash"])
            events=([(bar.symbol,bar) for bar in bars] if isinstance(bars,list) else list(bars.items()))
            for symbol,bar in sorted(events,key=lambda item:(item[1].day,item[0])):
                multiplier=float(getattr(bar,"share_multiplier",1.0) or 1.0)
                dividend=float(getattr(bar,"cash_dividend_per_share",0.0) or 0.0)
                if multiplier<=0:
                    raise ValueError(f"invalid share multiplier for {symbol}")
                if abs(multiplier-1.0)<1e-12 and abs(dividend)<1e-12:
                    continue
                effective_date=bar.day.isoformat()
                if db.execute("SELECT 1 FROM simulation_corporate_actions WHERE account_id=? AND symbol=? AND effective_date=?",
                              (account_id,symbol,effective_date)).fetchone():
                    continue
                held=db.execute("SELECT shares,avg_cost FROM simulation_positions WHERE account_id=? AND symbol=?",
                                (account_id,symbol)).fetchone()
                if held is None:
                    # Record the no-position event so a restart cannot apply a
                    # historical action to shares bought later that day.
                    old_shares=new_shares=0;cash_delta=0.0
                else:
                    old_shares=int(held["shares"])
                    new_shares=int(round(old_shares*multiplier))
                    cash_delta=old_shares*dividend
                    new_cost=(float(held["avg_cost"])*old_shares/new_shares
                              if new_shares else 0.0)
                    db.execute("UPDATE simulation_positions SET shares=?,avg_cost=?,updated_at=? WHERE account_id=? AND symbol=?",
                               (new_shares,new_cost,now,account_id,symbol))
                    cash+=cash_delta
                pending=db.execute("SELECT id,quantity,payload FROM simulation_ledger WHERE status='pending' AND symbol=?",
                                   (symbol,)).fetchall()
                for order in pending:
                    if order["quantity"] is None:continue
                    adjusted_quantity=int(round(int(order["quantity"])*multiplier))
                    order_payload=json.loads(order["payload"])
                    order_payload.update({"corporate_action_adjusted":True,
                                          "pre_action_requested_quantity":int(order["quantity"]),
                                          "requested_quantity":adjusted_quantity})
                    db.execute("UPDATE simulation_ledger SET quantity=?,payload=? WHERE id=?",
                               (adjusted_quantity,json.dumps(order_payload,ensure_ascii=False),order["id"]))
                payload={"event":"corporate_action","adj_factor_not_used_for_accounting":True,
                         "share_multiplier":multiplier,"cash_dividend_per_share":dividend,
                         "old_shares":old_shares,"new_shares":new_shares,
                         "cash_delta":round(cash_delta,2),"broker_connected":False}
                db.execute("INSERT INTO simulation_corporate_actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (account_id,symbol,effective_date,now,multiplier,dividend,old_shares,
                            new_shares,round(cash_delta,2),json.dumps(payload,ensure_ascii=False)))
                db.execute("INSERT OR IGNORE INTO simulation_ledger(run_key,event_time,effective_at,event_type,symbol,quantity,price,amount,fee,status,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (f"{run_key}:corporate-action:{effective_date}",now,now,"corporate_action",symbol,
                            new_shares-old_shares,None,round(cash_delta,2),0,"applied",json.dumps(payload,ensure_ascii=False)))
                applied.append({"symbol":symbol,"effective_date":effective_date,**payload})
            db.execute("UPDATE simulation_accounts SET cash=?,updated_at=? WHERE id=?",
                       (round(cash,2),now,account_id))
        return applied

    def write_down_terminal_positions(self, run_key: str, terminal: dict[str,str],
                                      account_id: str="model") -> list[dict]:
        """Conservatively remove positions proven permanently unpriceable."""
        outcomes=[];now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            for symbol,reason in sorted(terminal.items()):
                held=db.execute("SELECT shares,avg_cost,last_price FROM simulation_positions WHERE account_id=? AND symbol=?",
                                (account_id,symbol)).fetchone()
                if held is None:continue
                shares=int(held["shares"]);book_value=shares*float(held["avg_cost"])
                last_mark=(shares*float(held["last_price"]) if held["last_price"] is not None else None)
                payload={"event":"forced_risk_write_down","reason":reason,"recovery_price":0.0,
                         "book_value_written_down":round(book_value,2),
                         "last_mark_written_down":round(last_mark,2) if last_mark is not None else None,
                         "broker_connected":False}
                pending=db.execute("SELECT id,payload FROM simulation_ledger WHERE status='pending' AND symbol=?",
                                   (symbol,)).fetchall()
                for row in pending:
                    prior=json.loads(row["payload"]);prior.update({"cancel_reason":"terminal_position_write_down"})
                    db.execute("UPDATE simulation_ledger SET status='cancelled',payload=? WHERE id=?",
                               (json.dumps(prior,ensure_ascii=False),row["id"]))
                db.execute("DELETE FROM simulation_positions WHERE account_id=? AND symbol=?",(account_id,symbol))
                db.execute("INSERT OR IGNORE INTO simulation_ledger(run_key,event_time,effective_at,event_type,symbol,quantity,price,amount,fee,status,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (f"{run_key}:write-down",now,now,"forced_write_down",symbol,shares,0.0,0.0,0.0,
                            "written_down",json.dumps(payload,ensure_ascii=False)))
                outcomes.append({"symbol":symbol,"shares":shares,"status":"written_down","reason":reason,
                                 "book_value_written_down":round(book_value,2)})
        return outcomes

    @staticmethod
    def _fee(value: float, side: str) -> float:
        return max(5.0,value*.0003)+(value*.0005 if side=="sell" else 0)

    def match_pending(self, as_of: str, bars: dict[str,Any], account_id: str="model", slippage_bps: float=8.0) -> list[dict]:
        """Settle due virtual intents atomically. This method has no broker/network surface."""
        outcomes=[];now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            account=db.execute("SELECT * FROM simulation_accounts WHERE id=?",(account_id,)).fetchone()
            if account is None:return []
            cash=float(account["cash"])
            rows=db.execute("SELECT * FROM simulation_ledger WHERE status='pending' AND effective_at<=? ORDER BY id",(as_of,)).fetchall()
            for row in rows:
                payload=json.loads(row["payload"]);side=payload.get("side","buy");symbol=row["symbol"];bar=bars.get(symbol)
                # Absence from a supplied market slice is not proof that an
                # order was rejected.  Keep the intent pending until a bar or
                # an explicit suspension/limit state is observable.
                if bar is None:
                    continue
                status="rejected";reason="missing_bar";requested=0;filled=0;price=None;fee=0.0;value=0.0
                blocked=bar.suspended or (side=="buy" and bar.limit_up) or (side=="sell" and bar.limit_down)
                if blocked:
                    reason="suspended_or_price_limit"
                else:
                    price=bar.open*(1+(slippage_bps/10000 if side=="buy" else -slippage_bps/10000))
                    capacity=int((bar.volume*.01)//100)*100
                    held=db.execute("SELECT shares,avg_cost FROM simulation_positions WHERE account_id=? AND symbol=?",(account_id,symbol)).fetchone()
                    if side=="buy":
                        requested=(int(row["quantity"] or 0) or
                                   int(((row["amount"] or 0)/price)//100)*100)
                        affordable=int((max(0,cash-5)/(price*1.0003))//100)*100
                        filled=min(requested,capacity,affordable)
                    else:
                        requested=int(row["quantity"] or (held["shares"] if held else 0))
                        filled=min(requested,capacity,int(held["shares"] if held else 0))
                    if requested<=0:reason="below_board_lot_or_no_position"
                    elif filled<=0:reason="cash_or_capacity_unavailable"
                    else:
                        value=filled*price;fee=self._fee(value,side);status="filled" if filled==requested else "partial";reason="ok" if status=="filled" else "volume_or_cash_limited"
                        if side=="buy":
                            cash-=value+fee
                            old_shares=int(held["shares"]) if held else 0;old_cost=float(held["avg_cost"]) if held else 0
                            new_shares=old_shares+filled;avg=(old_shares*old_cost+value+fee)/new_shares
                            db.execute("INSERT INTO simulation_positions(account_id,symbol,shares,avg_cost,updated_at,last_price,last_price_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET shares=excluded.shares,avg_cost=excluded.avg_cost,updated_at=excluded.updated_at,last_price=excluded.last_price,last_price_at=excluded.last_price_at",
                                       (account_id,symbol,new_shares,avg,now,price,str(bar.day)))
                        else:
                            cash+=value-fee;remaining=int(held["shares"])-filled
                            if remaining:db.execute("UPDATE simulation_positions SET shares=?,updated_at=? WHERE account_id=? AND symbol=?",(remaining,now,account_id,symbol))
                            else:db.execute("DELETE FROM simulation_positions WHERE account_id=? AND symbol=?",(account_id,symbol))
                        if status=="partial":
                            remainder=requested-filled
                            continuation={**payload,"parent_ledger_id":row["id"],
                                          "requested_quantity":remainder,"continuation":True}
                            db.execute(
                                "INSERT OR IGNORE INTO simulation_ledger(run_key,event_time,effective_at,event_type,symbol,quantity,price,amount,fee,status,payload) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (row["run_key"],now,row["effective_at"],f"intent_{side}_remainder_{row['id']}",
                                 symbol,remainder,None,None,0,"pending",json.dumps(continuation,ensure_ascii=False)),
                            )
                payload.update({"requested_quantity":requested,"filled_quantity":filled,"reason":reason,"broker_connected":False})
                db.execute("UPDATE simulation_ledger SET quantity=?,price=?,amount=?,fee=?,status=?,payload=? WHERE id=?",
                           (filled,round(price,4) if price else None,round(value,2),round(fee,2),status,json.dumps(payload,ensure_ascii=False),row["id"]))
                outcomes.append({"id":row["id"],"symbol":symbol,"side":side,"status":status,"filled_quantity":filled,"reason":reason})
            db.execute("UPDATE simulation_accounts SET cash=?,updated_at=? WHERE id=?",(round(cash,2),now,account_id))
        return outcomes

    def mark_to_market(self, trade_date: str, closes: dict[str,float], payload: dict, account_id: str="model") -> dict:
        required=("model_version","provider","config_hash","settings_version","data_snapshot_hash")
        missing=[key for key in required if payload.get(key) in (None,"")]
        if missing:
            raise ValueError(f"simulation equity lineage missing: {', '.join(missing)}")
        now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            account=db.execute("SELECT * FROM simulation_accounts WHERE id=?",(account_id,)).fetchone()
            if account is None:
                raise ValueError("simulation account does not exist")
            for symbol,price in closes.items():
                db.execute("UPDATE simulation_positions SET last_price=?,last_price_at=?,updated_at=? WHERE account_id=? AND symbol=?",
                           (float(price),trade_date,now,account_id,symbol))
            positions=db.execute("SELECT symbol,shares,avg_cost,last_price,last_price_at FROM simulation_positions WHERE account_id=?",(account_id,)).fetchall()
            missing_closes=[x["symbol"] for x in positions if x["symbol"] not in closes]
            unpriced=[x["symbol"] for x in positions if x["symbol"] not in closes and x["last_price"] is None]
            market_value=sum(int(x["shares"])*(closes.get(x["symbol"]) if x["symbol"] in closes else
                             float(x["last_price"]) if x["last_price"] is not None else 0.0) for x in positions)
            cash=float(account["cash"]);equity=cash+market_value
            peak=max(float(account["peak_equity"]),equity);drawdown=equity/peak-1 if peak else 0
            db.execute("UPDATE simulation_accounts SET peak_equity=?,updated_at=? WHERE id=?",(peak,now,account_id))
            valuation_payload={**payload,"missing_closes":missing_closes,
                               "unpriced_symbols":unpriced,
                               "fallback":"last_observed_close" if missing_closes else None}
            db.execute("INSERT OR IGNORE INTO simulation_equity(account_id,trade_date,recorded_at,model_version,provider,config_hash,settings_version,data_snapshot_hash,cash,market_value,equity,drawdown,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                account_id,trade_date,now,str(payload["model_version"]),str(payload["provider"]),
                str(payload["config_hash"]),int(payload["settings_version"]),str(payload["data_snapshot_hash"]),
                round(cash,2),round(market_value,2),round(equity,2),round(drawdown,6),
                json.dumps(valuation_payload,ensure_ascii=False)))
        # Explicit accounting invariant, with cent-level tolerance.
        assert abs((cash+market_value)-equity)<.01
        return {"cash":round(cash,2),"market_value":round(market_value,2),"equity":round(equity,2),"drawdown":round(drawdown,6),
                "valuation_degraded":bool(missing_closes),"missing_closes":missing_closes,
                "unpriced_symbols":unpriced,"valuation_fallback":"last_observed_close" if missing_closes else None}

    def simulation(self) -> dict:
        with self.connect() as db:
            ledger=[dict(x) for x in db.execute("SELECT * FROM simulation_ledger ORDER BY id DESC LIMIT 200").fetchall()]
            equity=[dict(x) for x in db.execute("SELECT * FROM simulation_equity ORDER BY trade_date,recorded_at,id").fetchall()]
            account=db.execute("SELECT * FROM simulation_accounts WHERE id='model'").fetchone()
            positions=[dict(x) for x in db.execute("SELECT * FROM simulation_positions WHERE account_id='model' ORDER BY symbol").fetchall()]
        for x in ledger:x["payload"]=json.loads(x["payload"])
        for x in equity:
            x["payload"]=json.loads(x["payload"]);x["day"]=x["trade_date"]
        return {"account":dict(account) if account else None,"positions":positions,"ledger":ledger,"daily_equity":equity}

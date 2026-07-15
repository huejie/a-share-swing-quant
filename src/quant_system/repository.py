from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any


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
            CREATE TABLE IF NOT EXISTS simulation_ledger(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL, event_time TEXT NOT NULL,
              effective_at TEXT NOT NULL, event_type TEXT NOT NULL, symbol TEXT,
              quantity INTEGER, price REAL, amount REAL, fee REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL, payload TEXT NOT NULL,
              UNIQUE(run_key, event_type, symbol)
            );
            CREATE TABLE IF NOT EXISTS simulation_equity(
              trade_date TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, cash REAL NOT NULL,
              market_value REAL NOT NULL, equity REAL NOT NULL, drawdown REAL NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulation_accounts(
              id TEXT PRIMARY KEY, initial_capital REAL NOT NULL, cash REAL NOT NULL,
              peak_equity REAL NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulation_positions(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, shares INTEGER NOT NULL,
              avg_cost REAL NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id,symbol),
              FOREIGN KEY(account_id) REFERENCES simulation_accounts(id)
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
            """)

    def ping(self) -> bool:
        try:
            with self.connect() as db: return db.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error: return False

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
            db.execute("DELETE FROM simulation_equity")
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

    def append_simulation_intents(self, run_key: str, created_at: str, effective_at: str, intents: list[dict]):
        with self._lock,self.connect() as db:
            for p in intents:
                side=p["side"]
                payload={"label":"模拟意图","broker_connected":False,"side":side,"target_weight":p.get("target_weight",0),
                         "initial_weight":p.get("initial_weight",0),"requested_quantity":p.get("quantity")}
                db.execute("INSERT OR IGNORE INTO simulation_ledger(run_key,event_time,effective_at,event_type,symbol,quantity,price,amount,fee,status,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_key,created_at,effective_at,f"intent_{side}",p["symbol"],p.get("quantity"),None,p.get("amount"),0,"pending",json.dumps(payload,ensure_ascii=False)))

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
                            db.execute("INSERT INTO simulation_positions VALUES(?,?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET shares=excluded.shares,avg_cost=excluded.avg_cost,updated_at=excluded.updated_at",
                                       (account_id,symbol,new_shares,avg,now))
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
        now=datetime.now().astimezone().isoformat()
        with self._lock,self.connect() as db:
            account=db.execute("SELECT * FROM simulation_accounts WHERE id=?",(account_id,)).fetchone()
            positions=db.execute("SELECT symbol,shares,avg_cost FROM simulation_positions WHERE account_id=?",(account_id,)).fetchall()
            missing_closes=[x["symbol"] for x in positions if x["symbol"] not in closes]
            market_value=sum(int(x["shares"])*closes.get(x["symbol"],float(x["avg_cost"])) for x in positions)
            cash=float(account["cash"]);equity=cash+market_value
            peak=max(float(account["peak_equity"]),equity);drawdown=equity/peak-1 if peak else 0
            db.execute("UPDATE simulation_accounts SET peak_equity=?,updated_at=? WHERE id=?",(peak,now,account_id))
            valuation_payload={**payload,"missing_closes":missing_closes,
                               "fallback":"avg_cost" if missing_closes else None}
            db.execute("INSERT OR REPLACE INTO simulation_equity VALUES(?,?,?,?,?,?,?)",(trade_date,now,round(cash,2),round(market_value,2),round(equity,2),round(drawdown,6),json.dumps(valuation_payload,ensure_ascii=False)))
        # Explicit accounting invariant, with cent-level tolerance.
        assert abs((cash+market_value)-equity)<.01
        return {"cash":round(cash,2),"market_value":round(market_value,2),"equity":round(equity,2),"drawdown":round(drawdown,6),
                "valuation_degraded":bool(missing_closes),"missing_closes":missing_closes}

    def simulation(self) -> dict:
        with self.connect() as db:
            ledger=[dict(x) for x in db.execute("SELECT * FROM simulation_ledger ORDER BY id DESC LIMIT 200").fetchall()]
            equity=[dict(x) for x in db.execute("SELECT * FROM simulation_equity ORDER BY trade_date").fetchall()]
            account=db.execute("SELECT * FROM simulation_accounts WHERE id='model'").fetchone()
            positions=[dict(x) for x in db.execute("SELECT * FROM simulation_positions WHERE account_id='model' ORDER BY symbol").fetchall()]
        for x in ledger:x["payload"]=json.loads(x["payload"])
        for x in equity:
            x["payload"]=json.loads(x["payload"]);x["day"]=x["trade_date"]
        return {"account":dict(account) if account else None,"positions":positions,"ledger":ledger,"daily_equity":equity}

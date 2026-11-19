import os
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Importa las funciones de tu módulo mebest_wallet.py
from mebest_wallet import (
    BANK_NAME,
    MAX_CARDS_PER_ACCOUNT,
    create_card_internal,
    create_gift_code,
    fmt_mirs,
    get_installment_options,
    init_db,
    login_account,
    move_account_to_card,
    pay_later_with_plan,
    pay_off_plan_manual,
    pay_with_card,
    process_pay_later_engine,
    redeem_gift_code,
    register_account,
    sign_card_payload,
    transfer_between_accounts,
    verify_account_and_card_active,
)

app = FastAPI(title=f"{BANK_NAME} Private Bank API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = os.getenv("MEBEST_DB_PATH", "mebest_privado.db")


class RegisterIn(BaseModel):
    holder_name: str
    password: str
    first_card_label: str
    first_card_pin: str


class LoginIn(BaseModel):
    holder_name: str
    password: str


class CreateCardIn(BaseModel):
    account_id: str
    label: str
    pin: str


class FundCardIn(BaseModel):
    account_id: str
    card_id: str
    amount_mirs: float


class TransferAccountIn(BaseModel):
    from_account_id: str
    to_account_id: str
    amount_mirs: float
    note: str = "Transferencia MEBEST"


class PayCashIn(BaseModel):
    account_id: str
    card_id: str
    pin: str
    amount_mirs: float
    channel: str = "QR"
    merchant: str = "Comercio MEBEST"


class PayLaterIn(BaseModel):
    account_id: str
    card_id: str
    pin: str
    amount_mirs: float
    term_days: int
    recipient_account_id: Optional[str] = None
    channel: str = "QR"
    merchant: str = "Comercio MEBEST"


class PlanPaymentIn(BaseModel):
    account_id: str
    plan_id: str
    amount_mirs: float


class SimDaysIn(BaseModel):
    account_id: str
    extra_days: int


class GiftCreateIn(BaseModel):
    account_id: str
    card_id_for_pin: str
    pin: str
    amount_mirs: float


class GiftRedeemIn(BaseModel):
    target_account_id: str
    gift_code: str


@app.get("/")
def root():
    return {"bank": BANK_NAME, "status": "ONLINE"}


@app.post("/auth/register")
def api_register(data: RegisterIn):
    conn = init_db(DB_FILE)
    try:
        acc_id, card_id = register_account(
            conn, data.holder_name, data.password, data.first_card_label, data.first_card_pin
        )
        return {"account_id": acc_id, "card_id": card_id, "last4": card_id[-4:]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/auth/login")
def api_login(data: LoginIn):
    conn = init_db(DB_FILE)
    try:
        acc_id = login_account(conn, data.holder_name, data.password)
        msgs = process_pay_later_engine(conn, acc_id)
        return {"account_id": acc_id, "notices": msgs}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/dashboard/{account_id}")
def api_dashboard(account_id: str):
    conn = init_db(DB_FILE)
    try:
        notices = process_pay_later_engine(conn, account_id)
        acc = conn.execute(
            "SELECT account_id, holder_name, balance_mirs, status FROM accounts WHERE account_id = ?",
            (account_id.strip().upper(),),
        ).fetchone()
        if not acc:
            raise HTTPException(status_code=404, detail="Cuenta no encontrada")

        cards_rows = conn.execute(
            "SELECT card_id, label, balance_mirs, status FROM cards WHERE account_id = ? ORDER BY created_at ASC",
            (account_id.strip().upper(),),
        ).fetchall()

        cards = [
            {
                "number": idx,
                "label": r[1],
                "card_id": r[0],
                "last4": r[0][-4:],
                "balance_mirs": float(r[2]),
                "card_status": r[3],
                "account_status": acc[3],
            }
            for idx, r in enumerate(cards_rows, start=1)
        ]

        plans_rows = conn.execute(
            """
            SELECT plan_id, card_id, merchant_name, remaining_mirs, daily_base_mirs, term_days, status
            FROM pay_later_plans WHERE account_id = ? AND status = 'ACTIVE' ORDER BY created_at ASC
            """,
            (account_id.strip().upper(),),
        ).fetchall()

        plans = [
            {
                "plan_id": p[0],
                "card_id": p[1],
                "last4": p[1][-4:],
                "merchant": p[2],
                "remaining_mirs": float(p[3]),
                "daily_mirs": float(p[4]),
                "daily_pct": round(100.0 / p[5], 2),
                "term_days": p[5],
                "status": p[6],
            }
            for p in plans_rows
        ]

        txs = conn.execute(
            "SELECT created_at, tx_type, amount_mirs, card_id, channel, note FROM ledger WHERE account_id = ? ORDER BY rowid DESC LIMIT 20",
            (account_id.strip().upper(),),
        ).fetchall()

        return {
            "bank": BANK_NAME,
            "account_id": acc[0],
            "holder_name": acc[1],
            "balance_mirs": float(acc[2]),
            "account_status": acc[3],
            "max_cards": MAX_CARDS_PER_ACCOUNT,
            "total_debt_mirs": sum(p["remaining_mirs"] for p in plans),
            "cards": cards,
            "plans": plans,
            "notices": notices,
            "transactions": [
                {
                    "created_at": t[0],
                    "tx_type": t[1],
                    "amount_mirs": float(t[2]),
                    "amount_label": fmt_mirs(float(t[2])),
                    "card_id": t[3],
                    "channel": t[4],
                    "note": t[5],
                }
                for t in txs
            ],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/cards/create")
def api_create_card(data: CreateCardIn):
    conn = init_db(DB_FILE)
    try:
        with conn:
            cid = create_card_internal(conn, data.account_id, data.label, data.pin)
        return {"card_id": cid, "last4": cid[-4:]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/cards/fund")
def api_fund_card(data: FundCardIn):
    conn = init_db(DB_FILE)
    try:
        acc_b, card_b = move_account_to_card(conn, data.account_id, data.card_id, data.amount_mirs)
        return {"account_balance_mirs": acc_b, "card_balance_mirs": card_b}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/transfers/account")
def api_transfer_account(data: TransferAccountIn):
    conn = init_db(DB_FILE)
    try:
        from_b, to_b, absorbed = transfer_between_accounts(
            conn, data.from_account_id, data.to_account_id, data.amount_mirs, data.note
        )
        return {"from_balance_mirs": from_b, "to_balance_mirs": to_b, "absorbed_debt_mirs": absorbed}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/pay/cash")
def api_pay_cash(data: PayCashIn):
    conn = init_db(DB_FILE)
    try:
        card_b = pay_with_card(
            conn, data.account_id, data.card_id, data.pin, data.amount_mirs, data.channel, data.merchant
        )
        return {"card_balance_mirs": card_b}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/pay/later/options")
def api_later_options(amount_mirs: float):
    if amount_mirs <= 0:
        raise HTTPException(status_code=400, detail="Monto inválido")
    return {"options": get_installment_options(amount_mirs)}


@app.post("/pay/later/create")
def api_pay_later_create(data: PayLaterIn):
    conn = init_db(DB_FILE)
    try:
        plan_id, daily_mirs, total_due = pay_later_with_plan(
            conn,
            data.account_id,
            data.card_id,
            data.pin,
            data.amount_mirs,
            data.term_days,
            data.recipient_account_id,
            data.channel,
            data.merchant,
        )
        return {
            "plan_id": plan_id,
            "daily_mirs": daily_mirs,
            "daily_pct": round(100.0 / data.term_days, 2),
            "total_due_mirs": total_due,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/pay/later/abono")
def api_pay_later_abono(data: PlanPaymentIn):
    conn = init_db(DB_FILE)
    try:
        rem = pay_off_plan_manual(conn, data.account_id, data.plan_id, data.amount_mirs)
        return {"remaining_mirs": rem}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/pay/later/simulate-days")
def api_sim_days(data: SimDaysIn):
    conn = init_db(DB_FILE)
    try:
        msgs = process_pay_later_engine(conn, data.account_id, sim_extra_days=data.extra_days)
        return {"notices": msgs}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/gift/create")
def api_gift_create(data: GiftCreateIn):
    conn = init_db(DB_FILE)
    try:
        code, new_bal = create_gift_code(conn, data.account_id, data.card_id_for_pin, data.pin, data.amount_mirs)
        return {"gift_code": code, "account_balance_mirs": new_bal}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.post("/gift/redeem")
def api_gift_redeem(data: GiftRedeemIn):
    conn = init_db(DB_FILE)
    try:
        credited, final_bal, absorbed = redeem_gift_code(conn, data.target_account_id, data.gift_code)
        return {"credited_mirs": credited, "account_balance_mirs": final_bal, "absorbed_debt_mirs": absorbed}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/cards/{account_id}/{card_id}/credentials")
def api_card_creds(account_id: str, card_id: str):
    conn = init_db(DB_FILE)
    try:
        verify_account_and_card_active(conn, account_id, card_id)
        return sign_card_payload(card_id.strip().upper())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
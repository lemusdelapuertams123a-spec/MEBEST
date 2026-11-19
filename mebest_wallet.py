import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
import uuid

BANK_NAME = "MEBEST"
DB_PATH = "mebest_privado.db"
SESSION_FILE = ".mebest_session.json"
SECRET_KEY = b"clave-secreta-mebest-2026-cambia-esto"
MAX_CARDS_PER_ACCOUNT = 3
INITIAL_BALANCE_MIRS = 100
MAX_PLAN_DAYS = 7
PENALTY_MERCHANT_MIRS = 15
PENALTY_BANK_MIRS = 5
LATE_EXTRA_FEE_MIRS = 5
COMPOUND_INTERVAL_DAYS = 5
COMPOUND_RATE = 0.03


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            holder_name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance_mirs REAL NOT NULL DEFAULT 100.0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cards (
            card_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            label TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            balance_mirs REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(account_id)
        );

        CREATE TABLE IF NOT EXISTS pay_later_plans (
            plan_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            recipient_account_id TEXT,
            merchant_name TEXT NOT NULL,
            principal_mirs REAL NOT NULL,
            remaining_mirs REAL NOT NULL,
            daily_base_mirs REAL NOT NULL,
            term_days INTEGER NOT NULL,
            days_elapsed INTEGER NOT NULL DEFAULT 0,
            penalty_applied INTEGER NOT NULL DEFAULT 0,
            compound_cycles_applied INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at REAL NOT NULL,
            last_tick_at REAL NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(account_id),
            FOREIGN KEY (card_id) REFERENCES cards(card_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_token TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(account_id)
        );

        CREATE TABLE IF NOT EXISTS gift_codes (
            gift_code TEXT PRIMARY KEY,
            from_account_id TEXT NOT NULL,
            amount_mirs REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            redeemed_by_account_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            redeemed_at TIMESTAMP,
            FOREIGN KEY (from_account_id) REFERENCES accounts(account_id)
        );

        CREATE TABLE IF NOT EXISTS ledger (
            tx_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            card_id TEXT,
            amount_mirs REAL NOT NULL,
            tx_type TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'CLI',
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(account_id)
        );
        """
    )
    conn.commit()
    return conn


def fmt_mirs(val: float) -> str:
    if abs(val - round(val)) < 1e-6:
        return f"{int(round(val))} Mirs"
    return f"{val:.2f} Mirs"


def hash_secret(secret: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${digest.hex()}"


def verify_secret(secret: str, stored_hash: str) -> bool:
    try:
        salt, _ = stored_hash.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_secret(secret, salt), stored_hash)


def validate_four_digit_pin(pin: str) -> str:
    clean_pin = pin.strip()
    if not (clean_pin.isdigit() and len(clean_pin) == 4):
        raise ValueError("El PIN debe tener exactamente 4 dígitos numéricos (ej. 1234).")
    return clean_pin


def verify_account_active(conn: sqlite3.Connection, account_id: str) -> float:
    acc = conn.execute(
        "SELECT balance_mirs, status FROM accounts WHERE account_id = ?",
        (account_id.strip().upper(),),
    ).fetchone()
    if not acc:
        raise ValueError(f"Cuenta {account_id} no encontrada en {BANK_NAME}.")
    bal, status = acc
    if status != "ACTIVE":
        raise ValueError(f"La cuenta {account_id} no está activa (estado: {status}).")
    return float(bal)


def verify_account_and_card_active(conn: sqlite3.Connection, account_id: str, card_id: str) -> tuple[float, float]:
    acc_bal = verify_account_active(conn, account_id)

    card = conn.execute(
        "SELECT balance_mirs, status FROM cards WHERE card_id = ? AND account_id = ?",
        (card_id.strip().upper(), account_id.strip().upper()),
    ).fetchone()
    if not card:
        raise ValueError(f"La tarjeta {card_id} no existe o no pertenece a la cuenta {account_id}.")

    card_bal, card_status = card
    if card_status != "ACTIVE":
        raise ValueError(f"La tarjeta {card_id} no está activa (estado: {card_status}).")

    return float(acc_bal), float(card_bal)


def get_installment_options(amount_mirs: float) -> list[dict]:
    """Devuelve opciones de plazo de 1 a 7 días con porcentaje diario y monto diario al lado."""
    options = []
    for days in range(1, MAX_PLAN_DAYS + 1):
        pct = 100.0 / days
        daily_amt = amount_mirs / days
        options.append(
            {
                "days": days,
                "daily_pct": pct,
                "daily_mirs": daily_amt,
            }
        )
    return options


def print_installment_options(amount_mirs: float) -> None:
    print(f"\nOpciones de Pagar Después (máx. {MAX_PLAN_DAYS} días / 1 semana) para {fmt_mirs(amount_mirs)}:")
    for opt in get_installment_options(amount_mirs):
        label_dia = "día" if opt["days"] == 1 else "días"
        print(
            f"  - {opt['days']} {label_dia}: {opt['daily_pct']:.2f}% por día  |  "
            f"Por día: {fmt_mirs(opt['daily_mirs'])}"
        )


def _credit_recipient_if_any(conn: sqlite3.Connection, recipient_account_id: str | None, amount: float, note: str) -> None:
    if not recipient_account_id or amount <= 0:
        return
    rec = conn.execute(
        "SELECT balance_mirs, status FROM accounts WHERE account_id = ?",
        (recipient_account_id,),
    ).fetchone()
    if rec and rec[1] == "ACTIVE":
        new_rec_bal = float(rec[0]) + amount
        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_rec_bal, recipient_account_id))
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, NULL, ?, 'PAY_LATER_CREDIT', 'PAY_LATER', ?)",
            (str(uuid.uuid4()), recipient_account_id, amount, note),
        )


def _collect_from_account_or_card(
    conn: sqlite3.Connection,
    account_id: str,
    card_id: str,
    amount_needed: float,
    recipient_account_id: str | None,
    reason_note: str,
) -> float:
    """Intenta cobrar primero del saldo de la tarjeta y luego del saldo de la cuenta."""
    if amount_needed <= 0:
        return 0.0

    acc_bal, card_bal = verify_account_and_card_active(conn, account_id, card_id)
    collected = 0.0

    from_card = min(card_bal, amount_needed - collected)
    if from_card > 0:
        card_bal -= from_card
        collected += from_card
        conn.execute("UPDATE cards SET balance_mirs = ? WHERE card_id = ?", (card_bal, card_id))

    rem_need = amount_needed - collected
    if rem_need > 1e-9:
        from_acc = min(acc_bal, rem_need)
        if from_acc > 0:
            acc_bal -= from_acc
            collected += from_acc
            conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (acc_bal, account_id))

    if collected > 0:
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'AUTO_COLLECT', 'PAY_LATER', ?)",
            (str(uuid.uuid4()), account_id, card_id, collected, reason_note),
        )
        _credit_recipient_if_any(conn, recipient_account_id, collected, f"Abono automático: {reason_note}")

    return collected


def process_pay_later_engine(conn: sqlite3.Connection, account_id: str | None = None, sim_extra_days: int = 0) -> list[str]:
    """
    Evalúa planes 'Pagar después':
    - Cobra cuotas diarias automáticamente si hay saldo.
    - Tras superar 7 días sin liquidar: aplica castigo (+15 Mirs al destinatario + 5 Mirs al banco si estaba en 0,
      o +5 Mirs dividido 50/50 entre banco y usuario si hubo recargo vencido).
    - Cada 5 días vencidos multiplica el saldo pendiente por 1.03 (+3%).
    - Absorbe automáticamente cualquier saldo disponible si el usuario quedó en 0 y entra dinero.
    """
    messages: list[str] = []
    now = time.time() + (sim_extra_days * 86400)

    query = "SELECT * FROM pay_later_plans WHERE status = 'ACTIVE'"
    params: list = []
    if account_id:
        query += " AND account_id = ?"
        params.append(account_id.strip().upper())
    query += " ORDER BY created_at ASC"

    plans = conn.execute(query, params).fetchall()

    with conn:
        for row in plans:
            (
                plan_id,
                acc_id,
                cid,
                rec_acc_id,
                merchant,
                principal,
                remaining,
                daily_base,
                term_days,
                days_elapsed,
                penalty_applied,
                compound_cycles,
                status,
                created_at,
                last_tick_at,
            ) = row

            acc_bal, card_bal = verify_account_and_card_active(conn, acc_id, cid)
            elapsed_days_total = max(0, int((now - created_at) // 86400))
            new_days_to_tick = max(0, elapsed_days_total - days_elapsed)

            # 1) Cobro diario programado dentro del plazo
            for _ in range(new_days_to_tick):
                if remaining <= 1e-6:
                    break
                days_elapsed += 1
                cuota = min(daily_base, remaining)
                paid_now = _collect_from_account_or_card(
                    conn,
                    acc_id,
                    cid,
                    cuota,
                    rec_acc_id,
                    f"Cuota diaria ({days_elapsed}/{term_days}) plan {plan_id} ({merchant})",
                )
                remaining = max(0.0, remaining - paid_now)
                if paid_now < cuota - 1e-6:
                    messages.append(
                        f"Plan {plan_id}: cuota diaria de {fmt_mirs(cuota)} no cubierta por completo (cobrado: {fmt_mirs(paid_now)}). Restan {fmt_mirs(remaining)}."
                    )

            # 2) Si ya pasaron los 7 días (1 semana) y sigue habiendo deuda pendiente
            if elapsed_days_total > MAX_PLAN_DAYS and remaining > 1e-6:
                acc_bal, card_bal = verify_account_and_card_active(conn, acc_id, cid)
                total_available = acc_bal + card_bal

                if not penalty_applied:
                    if total_available <= 1e-6:
                        # Sin saldo en 0: castigo de +15 Mirs al que le ibas a pagar y +5 Mirs al banco
                        penalty_total = PENALTY_MERCHANT_MIRS + PENALTY_BANK_MIRS
                        remaining += penalty_total
                        penalty_applied = 1
                        conn.execute(
                            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'LATE_PENALTY_ZERO', 'PAY_LATER', ?)",
                            (
                                str(uuid.uuid4()),
                                acc_id,
                                cid,
                                penalty_total,
                                f"Castigo por vencer 7 días en 0 Mirs: +{PENALTY_MERCHANT_MIRS} Mirs a {rec_acc_id or merchant} y +{PENALTY_BANK_MIRS} Mirs a {BANK_NAME}. Nueva deuda: {fmt_mirs(remaining)}",
                            ),
                        )
                        messages.append(
                            f"CASTIGO (vencidos 7 días en 0 Mirs) en {plan_id}: se suman +{PENALTY_MERCHANT_MIRS} Mirs para el destinatario y +{PENALTY_BANK_MIRS} Mirs para {BANK_NAME}. Ahora debes {fmt_mirs(remaining)}."
                        )
                    else:
                        # Tiene saldo tras vencer los 7 días: absorbe deuda + recargo de 5 Mirs (mitad banco, mitad usuario)
                        extra_fee = min(LATE_EXTRA_FEE_MIRS, total_available)
                        half_bank = extra_fee / 2.0
                        half_user = extra_fee / 2.0
                        _collect_from_account_or_card(
                            conn,
                            acc_id,
                            cid,
                            extra_fee,
                            None,
                            f"Recargo por mora tras 7 días (+5 Mirs: {fmt_mirs(half_bank)} banco / {fmt_mirs(half_user)} usuario)",
                        )
                        acc_bal, card_bal = verify_account_and_card_active(conn, acc_id, cid)
                        absorb = min(remaining, acc_bal + card_bal)
                        if absorb > 0:
                            _collect_from_account_or_card(
                                conn,
                                acc_id,
                                cid,
                                absorb,
                                rec_acc_id,
                                f"Absorción automática por mora tras 7 días en plan {plan_id}",
                            )
                            remaining = max(0.0, remaining - absorb)
                        penalty_applied = 1
                        messages.append(
                            f"Mora tras 7 días en {plan_id}: cobro automático aplicado con recargo de {fmt_mirs(extra_fee)} ({fmt_mirs(half_bank)} {BANK_NAME} / {fmt_mirs(half_user)} usuario). Restan {fmt_mirs(remaining)}."
                        )

                # Capitalización del 3% cada 5 días de mora (después del día 7)
                days_past_grace = max(0, elapsed_days_total - MAX_PLAN_DAYS)
                target_cycles = days_past_grace // COMPOUND_INTERVAL_DAYS
                while compound_cycles < target_cycles and remaining > 1e-6:
                    compound_cycles += 1
                    remaining = remaining * (1.0 + COMPOUND_RATE)
                    conn.execute(
                        "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'COMPOUND_3PCT', 'PAY_LATER', ?)",
                        (
                            str(uuid.uuid4()),
                            acc_id,
                            cid,
                            remaining,
                            f"Multiplicador +3% cada 5 días (ciclo #{compound_cycles}) en plan {plan_id}. Deuda actual: {fmt_mirs(remaining)}",
                        ),
                    )
                    messages.append(
                        f"Multiplicador +3% (cada 5 días, ciclo #{compound_cycles}) en {plan_id}: deuda actualizada a {fmt_mirs(remaining)}."
                    )

            # 3) Absorción automática si hay saldo disponible y aún hay deuda pendiente
            if remaining > 1e-6:
                acc_bal, card_bal = verify_account_and_card_active(conn, acc_id, cid)
                avail = acc_bal + card_bal
                if avail > 0:
                    taken = _collect_from_account_or_card(
                        conn,
                        acc_id,
                        cid,
                        min(remaining, avail),
                        rec_acc_id,
                        f"Absorción automática de saldo disponible para plan {plan_id}",
                    )
                    remaining = max(0.0, remaining - taken)
                    if taken > 0:
                        messages.append(
                            f"Se absorbieron automáticamente {fmt_mirs(taken)} para el plan {plan_id}. Deuda pendiente: {fmt_mirs(remaining)}."
                        )

            new_status = "PAID" if remaining <= 1e-6 else "ACTIVE"
            conn.execute(
                """
                UPDATE pay_later_plans
                SET remaining_mirs = ?,
                    days_elapsed = ?,
                    penalty_applied = ?,
                    compound_cycles_applied = ?,
                    status = ?,
                    last_tick_at = ?
                WHERE plan_id = ?
                """,
                (remaining, days_elapsed, penalty_applied, compound_cycles, new_status, now, plan_id),
            )

    return messages


def absorb_incoming_funds_for_debts(conn: sqlite3.Connection, account_id: str) -> float:
    """Cuando entra dinero a una cuenta (recarga, transferencia o canje de regalo), absorbe automáticamente deudas de Pagar Después."""
    clean_acc = account_id.strip().upper()
    total_absorbed = 0.0

    plans = conn.execute(
        "SELECT plan_id, card_id, recipient_account_id, merchant_name, remaining_mirs FROM pay_later_plans WHERE account_id = ? AND status = 'ACTIVE' ORDER BY created_at ASC",
        (clean_acc,),
    ).fetchall()

    with conn:
        for plan_id, cid, rec_acc_id, merchant, remaining in plans:
            rem = float(remaining)
            if rem <= 1e-6:
                continue
            acc_bal = verify_account_active(conn, clean_acc)
            if acc_bal <= 1e-9:
                break
            take = min(acc_bal, rem)
            new_acc_bal = acc_bal - take
            new_rem = max(0.0, rem - take)
            total_absorbed += take

            conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_acc_bal, clean_acc))
            conn.execute(
                "UPDATE pay_later_plans SET remaining_mirs = ?, status = ? WHERE plan_id = ?",
                (new_rem, "PAID" if new_rem <= 1e-6 else "ACTIVE", plan_id),
            )
            conn.execute(
                "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'DEBT_ABSORBED', 'AUTO_ABSORB', ?)",
                (
                    str(uuid.uuid4()),
                    clean_acc,
                    cid,
                    take,
                    f"Saldo absorbido automáticamente para deuda {plan_id} ({merchant}). Debes aún: {fmt_mirs(new_rem)}",
                ),
            )
            _credit_recipient_if_any(conn, rec_acc_id, take, f"Abono automático recibido de {clean_acc} ({plan_id})")

    return total_absorbed


def pay_later_with_plan(
    conn: sqlite3.Connection,
    account_id: str,
    card_id: str,
    pin: str,
    amount_mirs: float,
    term_days: int,
    recipient_account_id: str | None = None,
    channel: str = "QR/NFC/BLE",
    merchant: str = "Comercio MEBEST",
) -> tuple[str, float, float]:
    if amount_mirs <= 0:
        raise ValueError("El monto debe ser mayor que 0 Mirs.")
    if not (1 <= term_days <= MAX_PLAN_DAYS):
        raise ValueError(f"El plazo debe ser de 1 a {MAX_PLAN_DAYS} días (máximo 1 semana).")
    valid_pin = validate_four_digit_pin(pin)

    clean_acc = account_id.strip().upper()
    clean_cid = card_id.strip().upper()
    clean_rec = recipient_account_id.strip().upper() if recipient_account_id and recipient_account_id.strip() else None

    if clean_rec:
        verify_account_active(conn, clean_rec)

    with conn:
        verify_account_and_card_active(conn, clean_acc, clean_cid)
        pin_hash = conn.execute(
            "SELECT pin_hash FROM cards WHERE card_id = ? AND account_id = ?",
            (clean_cid, clean_acc),
        ).fetchone()[0]

        if not verify_secret(valid_pin, pin_hash):
            raise ValueError("PIN de 4 dígitos incorrecto. Operación Pagar Después rechazada.")

        plan_id = f"PLN-{uuid.uuid4().hex[:10].upper()}"
        daily_base = amount_mirs / term_days
        now = time.time()

        conn.execute(
            """
            INSERT INTO pay_later_plans (
                plan_id, account_id, card_id, recipient_account_id, merchant_name,
                principal_mirs, remaining_mirs, daily_base_mirs, term_days,
                created_at, last_tick_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, clean_acc, clean_cid, clean_rec, merchant, amount_mirs, amount_mirs, daily_base, term_days, now, now),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'PAY_LATER_OPEN', ?, ?)",
            (
                str(uuid.uuid4()),
                clean_acc,
                clean_cid,
                amount_mirs,
                channel,
                f"Pagar después ({term_days} días, {100.0/term_days:.2f}%/día = {fmt_mirs(daily_base)}/día) en {merchant}",
            ),
        )

    return plan_id, daily_base, amount_mirs


def pay_off_plan_manual(conn: sqlite3.Connection, account_id: str, plan_id: str, amount_mirs: float) -> float:
    """Abono voluntario a un plan Pagar Después. Sin castigo si está dentro de los 7 días."""
    if amount_mirs <= 0:
        raise ValueError("El monto del abono debe ser mayor que 0 Mirs.")

    clean_acc = account_id.strip().upper()
    clean_plan = plan_id.strip().upper()

    with conn:
        acc_bal = verify_account_active(conn, clean_acc)
        plan = conn.execute(
            "SELECT remaining_mirs, recipient_account_id, merchant_name, status FROM pay_later_plans WHERE plan_id = ? AND account_id = ?",
            (clean_plan, clean_acc),
        ).fetchone()
        if not plan:
            raise ValueError("Plan de Pagar Después no encontrado en esta cuenta.")
        rem, rec_acc, merchant, status = plan
        if status != "ACTIVE" or rem <= 1e-6:
            raise ValueError("Este plan ya está liquidado o inactivo.")
        if acc_bal < amount_mirs:
            raise ValueError(f"Saldo insuficiente en cuenta ({fmt_mirs(acc_bal)} disponibles).")

        pay_amt = min(float(rem), float(amount_mirs))
        new_acc_bal = acc_bal - pay_amt
        new_rem = max(0.0, float(rem) - pay_amt)

        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_acc_bal, clean_acc))
        conn.execute(
            "UPDATE pay_later_plans SET remaining_mirs = ?, status = ? WHERE plan_id = ?",
            (new_rem, "PAID" if new_rem <= 1e-6 else "ACTIVE", clean_plan),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, NULL, ?, 'PAY_LATER_PAYMENT', 'CLI', ?)",
            (str(uuid.uuid4()), clean_acc, pay_amt, f"Abono a plan {clean_plan} ({merchant}). Resta: {fmt_mirs(new_rem)}"),
        )
        _credit_recipient_if_any(conn, rec_acc, pay_amt, f"Abono recibido de {clean_acc} ({clean_plan})")

    return new_rem


def sign_card_payload(card_id: str, ttl_seconds: int = 120) -> dict:
    exp = int(time.time()) + ttl_seconds
    sig_static = hmac.new(SECRET_KEY, f"{BANK_NAME}:{card_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    sig_ephemeral = hmac.new(SECRET_KEY, f"{BANK_NAME}:{card_id}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return {
        "bank": BANK_NAME,
        "card_id": card_id,
        "last4": card_id[-4:],
        "barcode_code128": card_id,
        "qr_static": f"{BANK_NAME}.{card_id}.{sig_static}",
        "qr_dynamic_ble_nfc": f"{BANK_NAME}:{card_id}:{exp}:{sig_ephemeral}",
        "expires_in_seconds": ttl_seconds,
    }


def export_card_codes(card_id: str) -> None:
    try:
        import barcode
        from barcode.writer import ImageWriter
        import qrcode
    except ImportError:
        print("Nota: instala 'qrcode[pil] python-barcode' si deseas exportar PNG de QR y código de barras.")
        return

    payloads = sign_card_payload(card_id)
    qrcode.make(payloads["qr_static"]).save(f"{card_id}_qr.png")
    code128 = barcode.get("code128", payloads["barcode_code128"], writer=ImageWriter())
    code128.save(f"{card_id}_barcode")
    print(f"Archivos generados: {card_id}_qr.png y {card_id}_barcode.png")


def create_card_internal(conn: sqlite3.Connection, account_id: str, label: str, pin: str) -> str:
    verify_account_active(conn, account_id)
    valid_pin = validate_four_digit_pin(pin)
    count = conn.execute("SELECT COUNT(*) FROM cards WHERE account_id = ?", (account_id,)).fetchone()[0]
    if count >= MAX_CARDS_PER_ACCOUNT:
        raise ValueError(f"Límite alcanzado: máximo {MAX_CARDS_PER_ACCOUNT} tarjetas por cuenta en {BANK_NAME}.")

    clean_label = label.strip() or f"Tarjeta {BANK_NAME} #{count + 1}"
    card_id = f"MBT-{uuid.uuid4().hex[:10].upper()}"
    pin_hashed = hash_secret(valid_pin, uuid.uuid4().hex)
    conn.execute(
        "INSERT INTO cards (card_id, account_id, label, pin_hash, balance_mirs) VALUES (?, ?, ?, ?, 0.0)",
        (card_id, account_id, clean_label, pin_hashed),
    )
    conn.execute(
        "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, note) VALUES (?, ?, ?, 0, 'CARD_ISSUED', ?)",
        (str(uuid.uuid4()), account_id, card_id, f"Emisión de tarjeta {clean_label} en {BANK_NAME}"),
    )
    return card_id


def register_account(
    conn: sqlite3.Connection,
    holder_name: str,
    password: str,
    first_card_label: str,
    first_card_pin: str,
) -> tuple[str, str]:
    holder_clean = holder_name.strip()
    if not holder_clean:
        raise ValueError("El nombre del titular no puede estar vacío.")
    if len(password) < 4:
        raise ValueError("La contraseña debe tener al menos 4 caracteres.")
    valid_pin = validate_four_digit_pin(first_card_pin)

    existing = conn.execute("SELECT account_id FROM accounts WHERE LOWER(holder_name) = LOWER(?)", (holder_clean,)).fetchone()
    if existing:
        raise ValueError("Ya existe una cuenta registrada con ese nombre (solo una cuenta por persona).")

    account_id = f"ACC-{uuid.uuid4().hex[:10].upper()}"
    pw_hashed = hash_secret(password, uuid.uuid4().hex)

    with conn:
        conn.execute(
            "INSERT INTO accounts (account_id, holder_name, password_hash, balance_mirs, status) VALUES (?, ?, ?, ?, 'ACTIVE')",
            (account_id, holder_clean, pw_hashed, float(INITIAL_BALANCE_MIRS)),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, note) VALUES (?, ?, NULL, ?, 'INITIAL_BONUS', ?)",
            (str(uuid.uuid4()), account_id, float(INITIAL_BALANCE_MIRS), f"Apertura de cuenta en {BANK_NAME} con 100 Mirs"),
        )
        card_id = create_card_internal(conn, account_id, first_card_label, valid_pin)

    return account_id, card_id


def save_session(conn: sqlite3.Connection, account_id: str) -> str:
    token = uuid.uuid4().hex
    with conn:
        conn.execute("UPDATE sessions SET active = 0 WHERE account_id = ?", (account_id,))
        conn.execute("INSERT INTO sessions (session_token, account_id, active) VALUES (?, ?, 1)", (token, account_id))
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump({"bank": BANK_NAME, "session_token": token, "account_id": account_id}, f)
    return token


def load_persisted_session(conn: sqlite3.Connection) -> str | None:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("session_token")
        account_id = data.get("account_id")
        if not token or not account_id:
            return None
        row = conn.execute(
            """
            SELECT s.account_id
            FROM sessions s
            JOIN accounts a ON a.account_id = s.account_id
            WHERE s.session_token = ? AND s.account_id = ? AND s.active = 1 AND a.status = 'ACTIVE'
            """,
            (token, account_id),
        ).fetchone()
        return row[0] if row else None
    except (OSError, json.JSONDecodeError, sqlite3.Error):
        return None


def logout_session(conn: sqlite3.Connection, account_id: str | None) -> None:
    if account_id:
        with conn:
            conn.execute("UPDATE sessions SET active = 0 WHERE account_id = ?", (account_id,))
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)


def login_account(conn: sqlite3.Connection, holder_name: str, password: str) -> str:
    row = conn.execute(
        "SELECT account_id, password_hash, status FROM accounts WHERE LOWER(holder_name) = LOWER(?)",
        (holder_name.strip(),),
    ).fetchone()
    if not row:
        raise ValueError("Cuenta no encontrada.")
    account_id, pw_hash, status = row
    if status != "ACTIVE":
        raise ValueError(f"La cuenta {account_id} no está activa ({status}).")
    if not verify_secret(password, pw_hash):
        raise ValueError("Contraseña incorrecta.")
    save_session(conn, account_id)
    return account_id


def move_account_to_card(conn: sqlite3.Connection, account_id: str, card_id: str, amount_mirs: float) -> tuple[float, float]:
    """Pasa Mirs de la cuenta a la tarjeta. Verifica cuenta y tarjeta activas; NO pide PIN."""
    if amount_mirs <= 0:
        raise ValueError("El monto debe ser mayor que 0 Mirs.")

    clean_acc = account_id.strip().upper()
    clean_cid = card_id.strip().upper()

    with conn:
        acc_bal, card_bal = verify_account_and_card_active(conn, clean_acc, clean_cid)
        if acc_bal < amount_mirs:
            raise ValueError(f"Saldo insuficiente en cuenta ({fmt_mirs(acc_bal)} disponibles).")

        new_acc_bal = acc_bal - amount_mirs
        new_card_bal = card_bal + amount_mirs

        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_acc_bal, clean_acc))
        conn.execute("UPDATE cards SET balance_mirs = ? WHERE card_id = ?", (new_card_bal, clean_cid))
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, note) VALUES (?, ?, ?, ?, 'FUND_CARD', 'Paso de saldo de cuenta a tarjeta sin PIN')",
            (str(uuid.uuid4()), clean_acc, clean_cid, amount_mirs),
        )

    return new_acc_bal, new_card_bal


def transfer_between_accounts(
    conn: sqlite3.Connection,
    from_account_id: str,
    to_account_id: str,
    amount_mirs: float,
    note: str = "Transferencia directa entre cuentas MEBEST",
) -> tuple[float, float, float]:
    """Transferencia solo con account_id (no funciona con card_id). Absorbe deuda si el destino tiene Pagar Después vencido."""
    from_id = from_account_id.strip().upper()
    to_id = to_account_id.strip().upper()

    if from_id.startswith("MBT-") or to_id.startswith("MBT-"):
        raise ValueError("Las transferencias solo funcionan con Número de cuenta (ACC-...), no con tarjeta (MBT-...).")
    if amount_mirs <= 0:
        raise ValueError("El monto a transferir debe ser mayor que 0 Mirs.")
    if from_id == to_id:
        raise ValueError("No puedes transferir a la misma cuenta.")

    with conn:
        from_bal = verify_account_active(conn, from_id)
        to_bal = verify_account_active(conn, to_id)

        if from_bal < amount_mirs:
            raise ValueError(f"Saldo insuficiente en {from_id} ({fmt_mirs(from_bal)} disponibles).")

        new_from_bal = from_bal - amount_mirs
        new_to_bal = to_bal + amount_mirs

        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_from_bal, from_id))
        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_to_bal, to_id))

        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, NULL, ?, 'TRANSFER_OUT', 'ACCOUNT_ID', ?)",
            (str(uuid.uuid4()), from_id, amount_mirs, f"{note} -> {to_id}"),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, NULL, ?, 'TRANSFER_IN', 'ACCOUNT_ID', ?)",
            (str(uuid.uuid4()), to_id, amount_mirs, f"{note} <- {from_id}"),
        )

    absorbed = absorb_incoming_funds_for_debts(conn, to_id)
    final_to_bal = verify_account_active(conn, to_id)
    return new_from_bal, final_to_bal, absorbed


def pay_with_card(
    conn: sqlite3.Connection,
    account_id: str,
    card_id: str,
    pin: str,
    amount_mirs: float,
    channel: str = "QR/NFC/BLE",
    merchant: str = "Comercio MEBEST",
) -> float:
    """Paga de contado con la tarjeta. Verifica cuenta activa, tarjeta activa y PIN de 4 dígitos."""
    if amount_mirs <= 0:
        raise ValueError("El monto a pagar debe ser mayor que 0 Mirs.")
    valid_pin = validate_four_digit_pin(pin)

    clean_acc = account_id.strip().upper()
    clean_cid = card_id.strip().upper()

    with conn:
        _, card_bal = verify_account_and_card_active(conn, clean_acc, clean_cid)
        pin_hash = conn.execute(
            "SELECT pin_hash FROM cards WHERE card_id = ? AND account_id = ?",
            (clean_cid, clean_acc),
        ).fetchone()[0]

        if not verify_secret(valid_pin, pin_hash):
            raise ValueError("PIN de 4 dígitos incorrecto. Pago rechazado.")
        if card_bal < amount_mirs:
            raise ValueError(f"Saldo insuficiente en tarjeta ({fmt_mirs(card_bal)} disponibles).")

        new_card_bal = card_bal - amount_mirs
        conn.execute("UPDATE cards SET balance_mirs = ? WHERE card_id = ?", (new_card_bal, clean_cid))
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'CARD_PAYMENT', ?, ?)",
            (str(uuid.uuid4()), clean_acc, clean_cid, amount_mirs, channel, f"Pago de contado en {merchant}"),
        )

    return new_card_bal


def create_gift_code(
    conn: sqlite3.Connection,
    account_id: str,
    card_id_for_pin: str,
    pin: str,
    amount_mirs: float,
) -> tuple[str, float]:
    if amount_mirs <= 0:
        raise ValueError("El monto del código de regalo debe ser mayor que 0 Mirs.")
    valid_pin = validate_four_digit_pin(pin)

    clean_acc = account_id.strip().upper()
    clean_cid = card_id_for_pin.strip().upper()

    with conn:
        acc_bal, _ = verify_account_and_card_active(conn, clean_acc, clean_cid)
        pin_hash = conn.execute(
            "SELECT pin_hash FROM cards WHERE card_id = ? AND account_id = ?",
            (clean_cid, clean_acc),
        ).fetchone()[0]

        if not verify_secret(valid_pin, pin_hash):
            raise ValueError("PIN de 4 dígitos incorrecto. No se generó el código de regalo.")
        if acc_bal < amount_mirs:
            raise ValueError(f"Saldo insuficiente en cuenta ({fmt_mirs(acc_bal)} disponibles).")

        new_acc_bal = acc_bal - amount_mirs
        gift_code = f"MBTGIFT-{uuid.uuid4().hex[:10].upper()}"

        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_acc_bal, clean_acc))
        conn.execute(
            "INSERT INTO gift_codes (gift_code, from_account_id, amount_mirs, status) VALUES (?, ?, ?, 'PENDING')",
            (gift_code, clean_acc, amount_mirs),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, ?, ?, 'GIFT_CREATED', 'GIFT_CODE', ?)",
            (str(uuid.uuid4()), clean_acc, clean_cid, amount_mirs, f"Código de regalo generado: {gift_code}"),
        )

    return gift_code, new_acc_bal


def redeem_gift_code(conn: sqlite3.Connection, target_account_id: str, gift_code: str) -> tuple[float, float, float]:
    clean_code = gift_code.strip().upper()
    clean_acc = target_account_id.strip().upper()

    with conn:
        acc_bal = verify_account_active(conn, clean_acc)
        gift = conn.execute(
            "SELECT from_account_id, amount_mirs, status FROM gift_codes WHERE gift_code = ?",
            (clean_code,),
        ).fetchone()
        if not gift:
            raise ValueError("Código de regalo no encontrado.")

        from_acc, amount_mirs, status = gift
        if status != "PENDING":
            raise ValueError(f"Este código ya fue utilizado o no está disponible (estado: {status}).")

        new_acc_bal = acc_bal + float(amount_mirs)
        conn.execute("UPDATE accounts SET balance_mirs = ? WHERE account_id = ?", (new_acc_bal, clean_acc))
        conn.execute(
            "UPDATE gift_codes SET status = 'REDEEMED', redeemed_by_account_id = ?, redeemed_at = CURRENT_TIMESTAMP WHERE gift_code = ?",
            (clean_acc, clean_code),
        )
        conn.execute(
            "INSERT INTO ledger (tx_id, account_id, card_id, amount_mirs, tx_type, channel, note) VALUES (?, ?, NULL, ?, 'GIFT_REDEEMED', 'GIFT_CODE', ?)",
            (str(uuid.uuid4()), clean_acc, float(amount_mirs), f"Canje de código {clean_code} (emitido por {from_acc})"),
        )

    absorbed = absorb_incoming_funds_for_debts(conn, clean_acc)
    final_bal = verify_account_active(conn, clean_acc)
    return float(amount_mirs), final_bal, absorbed


def show_dashboard(conn: sqlite3.Connection, account_id: str) -> None:
    acc = conn.execute(
        "SELECT account_id, holder_name, balance_mirs, status FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    cards = conn.execute(
        "SELECT card_id, label, balance_mirs, status FROM cards WHERE account_id = ? ORDER BY created_at ASC",
        (account_id,),
    ).fetchall()
    plans = conn.execute(
        "SELECT plan_id, card_id, merchant_name, remaining_mirs, daily_base_mirs, term_days, status FROM pay_later_plans WHERE account_id = ? AND status = 'ACTIVE'",
        (account_id,),
    ).fetchall()

    total_debt = sum(float(p[3]) for p in plans)

    print("\n" + "=" * 82)
    print(f"BANCO: {BANK_NAME}  |  TITULAR: {acc[1]}")
    print(
        f"Número de cuenta: {acc[0]} ({acc[3]})  |  Saldo cuenta: {fmt_mirs(float(acc[2]))}  |  "
        f"Deuda Pagar Después: {fmt_mirs(total_debt)}"
    )
    print(f"Tarjetas ({len(cards)}/{MAX_CARDS_PER_ACCOUNT}):")
    if not cards:
        print("  (Sin tarjetas registradas)")
    else:
        for idx, (cid, label, cbal, st) in enumerate(cards, start=1):
            print(
                f"  No. {idx} ({label}) -> Número de tarjeta: {cid} "
                f"(Termina en: {cid[-4:]}) | Saldo: {fmt_mirs(float(cbal))} | Tarjeta: {st} | Cuenta: {acc[3]}"
            )
    if plans:
        print("Planes activos Pagar Después (máx. 7 días):")
        for pid, cid, merch, rem, d_base, t_days, st in plans:
            pct = 100.0 / t_days
            print(
                f"  - [{pid}] Tarjeta {cid[-4:]} | {merch} | Plazo: {t_days}d ({pct:.2f}%/día = {fmt_mirs(float(d_base))}/día) | "
                f"Pendiente: {fmt_mirs(float(rem))} ({st})"
            )
    print("=" * 82)


def run_cli() -> None:
    conn = init_db()
    current_account_id = load_persisted_session(conn)

    if current_account_id:
        msgs = process_pay_later_engine(conn, current_account_id)
        print(f"[{BANK_NAME}] Sesión restaurada automáticamente. Número de cuenta: {current_account_id}")
        for m in msgs:
            print("  [AVISO]", m)

    while True:
        if not current_account_id:
            print(f"\n--- BANCO PRIVADO {BANK_NAME} (MIRS) ---")
            print("1. Registrarse (crea tu Número de cuenta con 100 Mirs + 1ra tarjeta con PIN de 4 dígitos)")
            print("2. Iniciar sesión")
            print("0. Salir")
            op = input("Elige una opción: ").strip()

            try:
                if op == "1":
                    name = input("Nombre completo: ")
                    pw = input("Crea una contraseña: ")
                    label = input("Nombre/etiqueta de tu primera tarjeta: ")
                    pin = input("Elige un PIN de 4 dígitos para esta tarjeta: ")
                    acc_id, card_id = register_account(conn, name, pw, label, pin)
                    save_session(conn, acc_id)
                    current_account_id = acc_id
                    print(
                        f"\n¡Bienvenido a {BANK_NAME}! Número de cuenta: {acc_id} (100 Mirs) | "
                        f"Tarjeta No. 1: {card_id} (Termina en: {card_id[-4:]})"
                    )
                elif op == "2":
                    name = input("Nombre del titular: ")
                    pw = input("Contraseña: ")
                    current_account_id = login_account(conn, name, pw)
                    msgs = process_pay_later_engine(conn, current_account_id)
                    print(f"\nSesión iniciada en {BANK_NAME}. Número de cuenta: {current_account_id}")
                    for m in msgs:
                        print("  [AVISO]", m)
                elif op == "0":
                    break
                else:
                    print("Opción no válida.")
            except ValueError as err:
                print(f"Error: {err}")
        else:
            show_dashboard(conn, current_account_id)
            print("1. Crear otra tarjeta (máx. 3 por cuenta, elige PIN de 4 dígitos)")
            print("2. Pasar Mirs de la Cuenta a una Tarjeta (NO pide PIN)")
            print("3. Transferir Mirs a otra Cuenta (SOLO con Número de cuenta ACC-...)")
            print("4. Pagar de contado con una Tarjeta (SÍ pide PIN de 4 dígitos)")
            print("5. Pagar después con una Tarjeta (1 a 7 días, % y monto diario, castigo tras 7d + 3%/5d)")
            print("6. Abonar a un plan Pagar Después / Simular avance de días para probar reglas")
            print("7. Generar código de regalo / transferencia segura (pide Cuenta, Monto y PIN)")
            print("8. Canjear código de regalo en una Cuenta (saldo al instante / absorbe si hay deuda)")
            print("9. Ver credenciales QR / Código de barras / NFC / BLE de una tarjeta")
            print("10. Ver historial de movimientos")
            print("11. Cerrar sesión")
            print("0. Salir (mantiene sesión iniciada)")
            op = input("Elige una opción: ").strip()

            try:
                if op == "1":
                    label = input("Etiqueta de la nueva tarjeta: ")
                    pin = input("Elige un PIN de 4 dígitos para esta tarjeta: ")
                    with conn:
                        new_cid = create_card_internal(conn, current_account_id, label, pin)
                    print(f"Nueva tarjeta {BANK_NAME} creada: {new_cid} (Termina en: {new_cid[-4:]})")
                elif op == "2":
                    cid = input("Ingresa el Número de tarjeta destino: ").strip()
                    amt = float(input("Cantidad de Mirs a pasar desde tu cuenta: "))
                    acc_b, card_b = move_account_to_card(conn, current_account_id, cid, amt)
                    print(f"Listo (sin PIN). Saldo cuenta: {fmt_mirs(acc_b)} | Saldo tarjeta: {fmt_mirs(card_b)}")
                elif op == "3":
                    to_acc = input("Ingresa el Número de cuenta destino (ACC-...): ").strip()
                    amt = float(input("Cantidad de Mirs a transferir: "))
                    from_b, to_b, absorbed = transfer_between_accounts(conn, current_account_id, to_acc, amt)
                    print(
                        f"Transferencia completada. Tu saldo en cuenta: {fmt_mirs(from_b)} | "
                        f"Saldo destino: {fmt_mirs(to_b)}"
                    )
                    if absorbed > 0:
                        print(f"Nota: se absorbieron automáticamente {fmt_mirs(absorbed)} en {to_acc} por deuda Pagar Después.")
                elif op == "4":
                    cid = input("Ingresa el Número de tarjeta con el que vas a pagar: ").strip()
                    amt = float(input("Monto a pagar en Mirs: "))
                    pin = input("Ingresa el PIN de 4 dígitos de la tarjeta: ").strip()
                    ch = input("Canal (QR / BARRAS / NFC_ANDROID / BLE): ").strip() or "QR"
                    new_cb = pay_with_card(conn, current_account_id, cid, pin, amt, channel=ch)
                    print(f"Pago de contado aprobado en {BANK_NAME}. Saldo restante en tarjeta: {fmt_mirs(new_cb)}")
                elif op == "5":
                    cid = input("Ingresa el Número de tarjeta para Pagar Después: ").strip()
                    amt = float(input("Monto total a pagar después en Mirs: "))
                    print_installment_options(amt)
                    days = int(input("Elige el plazo en días (1 a 7): ").strip())
                    rec_acc = input("Cuenta destino/comercio (opcional, ej. ACC-... o Enter): ").strip() or None
                    merch = input("Concepto/comercio [Comercio MEBEST]: ").strip() or "Comercio MEBEST"
                    pin = input("Ingresa el PIN de 4 dígitos de la tarjeta: ").strip()
                    ch = input("Canal (QR / BARRAS / NFC_ANDROID / BLE): ").strip() or "QR"
                    plan_id, daily_mirs, total_due = pay_later_with_plan(
                        conn, current_account_id, cid, pin, amt, days, recipient_account_id=rec_acc, channel=ch, merchant=merch
                    )
                    print(
                        f"\nPlan {plan_id} creado ({days} días | {100.0/days:.2f}% diario = {fmt_mirs(daily_mirs)}/día). "
                        f"Total por pagar después: {fmt_mirs(total_due)}"
                    )
                elif op == "6":
                    print("a) Abonar saldo a un plan Pagar Después")
                    print("b) Simular avance de días (probar cobro diario, castigo tras 7d y +3% cada 5d)")
                    sub = input("Elige (a/b): ").strip().lower()
                    if sub == "a":
                        pid = input("Ingresa el ID del plan (PLN-...): ").strip()
                        amt = float(input("Monto en Mirs a abonar: "))
                        rem = pay_off_plan_manual(conn, current_account_id, pid, amt)
                        print(f"Abono aplicado. Restante en el plan: {fmt_mirs(rem)}")
                    elif sub == "b":
                        extra_d = int(input("¿Cuántos días simular que avanzan? (ej. 1, 7, 8, 13): ").strip())
                        msgs = process_pay_later_engine(conn, current_account_id, sim_extra_days=extra_d)
                        print(f"Simulación de +{extra_d} días ejecutada.")
                        for m in msgs:
                            print("  ->", m)
                    else:
                        print("Sub-opción no válida.")
                elif op == "7":
                    src_acc = input(f"Número de cuenta origen [{current_account_id}]: ").strip() or current_account_id
                    amt = float(input("Monto en Mirs a apartar para el código de regalo: "))
                    cid_pin = input("Número de tarjeta de esa cuenta para validar PIN: ").strip()
                    pin = input("Ingresa el PIN de 4 dígitos: ").strip()
                    code, new_bal = create_gift_code(conn, src_acc, cid_pin, pin, amt)
                    print(f"\nCódigo de regalo generado: {code}")
                    print(f"Monto apartado: {fmt_mirs(amt)} | Saldo restante en cuenta {src_acc}: {fmt_mirs(new_bal)}")
                elif op == "8":
                    dst_acc = input(f"Número de cuenta donde canjear [{current_account_id}]: ").strip() or current_account_id
                    code = input("Ingresa el código de regalo (MBTGIFT-...): ").strip()
                    credited, final_bal, absorbed = redeem_gift_code(conn, dst_acc, code)
                    print(f"¡Canje exitoso! Se acreditaron {fmt_mirs(credited)} al instante.")
                    if absorbed > 0:
                        print(f"Se absorbieron automáticamente {fmt_mirs(absorbed)} por deuda Pagar Después.")
                    print(f"Saldo actual en {dst_acc}: {fmt_mirs(final_bal)}")
                elif op == "9":
                    cid = input("Ingresa el Número de tarjeta: ").strip()
                    verify_account_and_card_active(conn, current_account_id, cid)
                    info = sign_card_payload(cid)
                    print(json.dumps(info, indent=2, ensure_ascii=False))
                    gen_img = input("¿Generar imágenes PNG de QR y código de barras? (s/n): ").strip().lower()
                    if gen_img == "s":
                        export_card_codes(cid)
                elif op == "10":
                    rows = conn.execute(
                        "SELECT created_at, tx_type, amount_mirs, card_id, channel, note FROM ledger WHERE account_id = ? ORDER BY rowid DESC LIMIT 15",
                        (current_account_id,),
                    ).fetchall()
                    print(f"\n--- ÚLTIMOS MOVIMIENTOS ({BANK_NAME}) ---")
                    for r in rows:
                        print(f"{r[0]} | {r[1]} | {fmt_mirs(float(r[2]))} | Tarjeta: {r[3] or '-'} | Canal: {r[4]} | {r[5]}")
                elif op == "11":
                    logout_session(conn, current_account_id)
                    current_account_id = None
                    print("Sesión cerrada.")
                elif op == "0":
                    break
                else:
                    print("Opción no válida.")
            except ValueError as err:
                print(f"Error: {err}")


if __name__ == "__main__":
    run_cli()
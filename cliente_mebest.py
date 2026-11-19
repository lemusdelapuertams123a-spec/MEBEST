import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
import webbrowser

LOCAL_PORT = 8765
CONFIG_FILE = ".mebest_remote_config.json"
DEFAULT_REMOTE_URL = "http://127.0.0.1:8000"  # Cámbialo por la URL de tu servidor en línea gratis


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"remote_url": DEFAULT_REMOTE_URL, "account_id": ""}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


HTML_UI = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>MEBEST — Banca Privada Mirs</title>
<style>
  :root {
    --bg: #0b0f19;
    --card: #12192b;
    --card-2: #18223a;
    --accent: #38bdf8;
    --accent-2: #818cf8;
    --ok: #22c55e;
    --warn: #f59e0b;
    --err: #ef4444;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --border: rgba(148,163,184,0.16);
  }
  * { box-sizing: border-box; font-family: "Segoe UI", system-ui, sans-serif; }
  body {
    margin: 0; background: radial-gradient(circle at top right, #172554, var(--bg) 55%);
    color: var(--text); min-height: 100vh; padding: 24px;
  }
  .wrap { max-width: 1120px; margin: 0 auto; }
  .topbar {
    display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center;
    background: var(--card); border: 1px solid var(--border); padding: 16px 20px; border-radius: 16px;
    margin-bottom: 20px; box-shadow: 0 12px 30px rgba(0,0,0,0.35);
  }
  .brand { font-size: 20px; font-weight: 800; letter-spacing: 1px; color: var(--accent); }
  .url-box { display: flex; gap: 8px; flex: 1; max-width: 460px; }
  input, select, button {
    border-radius: 10px; border: 1px solid var(--border); padding: 10px 12px;
    background: var(--card-2); color: var(--text); font-size: 14px;
  }
  input { width: 100%; }
  button {
    cursor: pointer; font-weight: 600;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #061021; border: none; transition: transform .12s ease, opacity .15s ease;
  }
  button:hover { transform: translateY(-1px); opacity: 0.95; }
  button.secondary { background: var(--card-2); color: var(--text); border: 1px solid var(--border); }
  .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 18px; }
  .panel {
    background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 18px;
    box-shadow: 0 10px 24px rgba(0,0,0,0.28);
  }
  .panel h3 { margin: 0 0 12px; font-size: 16px; color: var(--accent); }
  .kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .kpi { background: var(--card-2); border: 1px solid var(--border); border-radius: 14px; padding: 14px; }
  .kpi small { color: var(--muted); display: block; margin-bottom: 4px; }
  .kpi strong { font-size: 20px; }
  .card-item {
    background: linear-gradient(135deg, #1e293b, #0f172a);
    border: 1px solid rgba(56,189,248,0.35); border-radius: 14px; padding: 14px; margin-bottom: 10px;
  }
  .row { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
  .toast {
    padding: 12px 14px; border-radius: 12px; margin-bottom: 14px; font-size: 14px; display: none;
  }
  .toast.ok { display: block; background: rgba(34,197,94,0.16); border: 1px solid var(--ok); color: #bbf7d0; }
  .toast.err { display: block; background: rgba(239,68,68,0.16); border: 1px solid var(--err); color: #fecaca; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); }
  .hidden { display: none; }
  pre { background: #070b14; padding: 12px; border-radius: 10px; overflow-x: auto; font-size: 12px; color: #7dd3fc; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">MEBEST • MIRS</div>
    <div class="url-box">
      <input id="remoteUrl" placeholder="URL del servidor en línea (ej. https://mebest.onrender.com)" />
      <button class="secondary" onclick="saveRemoteUrl()">Conectar</button>
    </div>
    <button id="btnLogout" class="secondary hidden" onclick="logout()">Cerrar sesión</button>
  </div>

  <div id="toast" class="toast"></div>

  <!-- AUTH -->
  <div id="authSection" class="grid-2">
    <div class="panel">
      <h3>Registrarse en MEBEST</h3>
      <div class="row"><input id="regName" placeholder="Nombre completo" /></div>
      <div class="row"><input id="regPass" type="password" placeholder="Contraseña" /></div>
      <div class="row"><input id="regCardLabel" placeholder="Nombre de tu 1ra tarjeta (ej. Principal)" /></div>
      <div class="row"><input id="regCardPin" maxlength="4" placeholder="PIN de 4 dígitos (ej. 1234)" /></div>
      <button onclick="registerAcc()">Crear cuenta (100 Mirs + 1ra tarjeta)</button>
    </div>
    <div class="panel">
      <h3>Iniciar sesión</h3>
      <div class="row"><input id="loginName" placeholder="Nombre del titular" /></div>
      <div class="row"><input id="loginPass" type="password" placeholder="Contraseña" /></div>
      <button onclick="loginAcc()">Entrar a mi cuenta</button>
    </div>
  </div>

  <!-- DASHBOARD -->
  <div id="appSection" class="hidden">
    <div class="kpi-row">
      <div class="kpi"><small>Titular</small><strong id="kHolder">-</strong></div>
      <div class="kpi"><small>Número de cuenta</small><strong id="kAcc">-</strong></div>
      <div class="kpi"><small>Saldo en cuenta</small><strong id="kBal">0 Mirs</strong></div>
      <div class="kpi"><small>Deuda Pagar Después</small><strong id="kDebt" style="color:var(--warn)">0 Mirs</strong></div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>Mis Tarjetas (máx. 3 por cuenta)</h3>
        <div id="cardsList"></div>
        <div class="row">
          <input id="newCardLabel" placeholder="Nombre nueva tarjeta" />
          <input id="newCardPin" maxlength="4" placeholder="PIN 4 dígitos" style="max-width:130px" />
          <button onclick="createCard()">+ Crear tarjeta</button>
        </div>
        <div class="row">
          <input id="credCardId" placeholder="Card ID (MBT-...)" />
          <button class="secondary" onclick="loadCreds()">Ver QR / Barras / NFC / BLE</button>
        </div>
        <pre id="credsBox" class="hidden"></pre>
      </div>

      <div class="panel">
        <h3>Mover a Tarjeta (sin PIN) & Transferir por Cuenta</h3>
        <div class="row">
          <input id="fundCardId" placeholder="Card ID destino (MBT-...)" />
          <input id="fundAmt" type="number" step="0.01" placeholder="Mirs" style="max-width:110px" />
          <button onclick="fundCard()">Pasar a tarjeta</button>
        </div>
        <hr style="border-color:var(--border); margin:12px 0;" />
        <div class="row">
          <input id="trToAcc" placeholder="Cuenta destino (SOLO ACC-...)" />
          <input id="trAmt" type="number" step="0.01" placeholder="Mirs" style="max-width:110px" />
          <button onclick="transferAcc()">Transferir</button>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>Pagar de Contado o Pagar Después (1–7 días)</h3>
        <div class="row">
          <input id="payCardId" placeholder="Card ID (MBT-...)" />
          <input id="payAmt" type="number" step="0.01" placeholder="Monto Mirs" style="max-width:120px" />
          <input id="payPin" maxlength="4" placeholder="PIN 4d" style="max-width:95px" />
        </div>
        <div class="row">
          <select id="payChannel">
            <option value="QR">QR</option>
            <option value="BARRAS">Código de barras</option>
            <option value="NFC_ANDROID">NFC (Android)</option>
            <option value="BLE">Bluetooth (BLE)</option>
          </select>
          <button class="secondary" onclick="payCash()">Pagar contado</button>
        </div>
        <div class="row">
          <select id="laterDays" style="max-width:170px">
            <option value="1">1 día (100%/día)</option>
            <option value="2">2 días (50%/día)</option>
            <option value="3">3 días (33.33%/día)</option>
            <option value="4">4 días (25%/día)</option>
            <option value="5">5 días (20%/día)</option>
            <option value="6">6 días (16.67%/día)</option>
            <option value="7" selected>7 días (14.29%/día)</option>
          </select>
          <input id="laterRecAcc" placeholder="Cuenta destino (opcional ACC-...)" />
          <button onclick="payLater()">Pagar después</button>
        </div>
        <div id="plansList"></div>
        <div class="row">
          <input id="simDays" type="number" value="1" style="max-width:90px" />
          <button class="secondary" onclick="simAdvance()">Simular días (+días)</button>
        </div>
      </div>

      <div class="panel">
        <h3>Código de Regalo (Transferencia Segura)</h3>
        <div class="row">
          <input id="giftCardPin" placeholder="Card ID para validar PIN" />
          <input id="giftPin" maxlength="4" placeholder="PIN 4d" style="max-width:95px" />
          <input id="giftAmt" type="number" step="0.01" placeholder="Mirs" style="max-width:105px" />
          <button onclick="makeGift()">Generar código</button>
        </div>
        <div id="giftOut" style="color:var(--ok); margin-bottom:10px; font-weight:700;"></div>
        <div class="row">
          <input id="redeemCode" placeholder="Código MBTGIFT-..." />
          <button onclick="redeemGift()">Canjear en mi cuenta</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <h3>Movimientos recientes</h3>
      <table>
        <thead><tr><th>Fecha</th><th>Tipo</th><th>Monto</th><th>Tarjeta</th><th>Canal</th><th>Detalle</th></tr></thead>
        <tbody id="txBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
let STATE = { remote_url: "", account_id: "" };

function showToast(msg, isErr=false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast " + (isErr ? "err" : "ok");
}

async function initClient() {
  const res = await fetch("/api/config");
  STATE = await res.json();
  document.getElementById("remoteUrl").value = STATE.remote_url || "";
  if (STATE.account_id) {
    await refreshDashboard();
  }
}

async function saveRemoteUrl() {
  STATE.remote_url = document.getElementById("remoteUrl").value.trim().replace(/\\/+$/, "");
  await fetch("/api/config", { method: "POST", body: JSON.stringify(STATE) });
  showToast("Servidor conectado: " + STATE.remote_url);
  if (STATE.account_id) refreshDashboard();
}

async function api(path, method="GET", body=null) {
  const base = document.getElementById("remoteUrl").value.trim().replace(/\\/+$/, "");
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(base + path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Error en servidor");
  return data;
}

async function registerAcc() {
  try {
    const d = await api("/auth/register", "POST", {
      holder_name: document.getElementById("regName").value,
      password: document.getElementById("regPass").value,
      first_card_label: document.getElementById("regCardLabel").value,
      first_card_pin: document.getElementById("regCardPin").value
    });
    STATE.account_id = d.account_id;
    await fetch("/api/config", { method: "POST", body: JSON.stringify(STATE) });
    showToast("Cuenta creada: " + d.account_id);
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function loginAcc() {
  try {
    const d = await api("/auth/login", "POST", {
      holder_name: document.getElementById("loginName").value,
      password: document.getElementById("loginPass").value
    });
    STATE.account_id = d.account_id;
    await fetch("/api/config", { method: "POST", body: JSON.stringify(STATE) });
    showToast("Sesión iniciada: " + d.account_id);
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function logout() {
  STATE.account_id = "";
  await fetch("/api/config", { method: "POST", body: JSON.stringify(STATE) });
  document.getElementById("appSection").classList.add("hidden");
  document.getElementById("btnLogout").classList.add("hidden");
  document.getElementById("authSection").classList.remove("hidden");
}

async function refreshDashboard() {
  try {
    const d = await api("/dashboard/" + STATE.account_id);
    document.getElementById("authSection").classList.add("hidden");
    document.getElementById("appSection").classList.remove("hidden");
    document.getElementById("btnLogout").classList.remove("hidden");

    document.getElementById("kHolder").textContent = d.holder_name;
    document.getElementById("kAcc").textContent = d.account_id;
    document.getElementById("kBal").textContent = d.balance_mirs + " Mirs";
    document.getElementById("kDebt").textContent = d.total_debt_mirs + " Mirs";

    const cardsDiv = document.getElementById("cardsList");
    cardsDiv.innerHTML = d.cards.map(c =>
      `<div class="card-item">
        <strong>No. ${c.number} (${c.label})</strong> — ${c.card_id} <em>(Termina en: ${c.last4})</em><br/>
        Saldo: <strong>${c.balance_mirs} Mirs</strong> | Tarjeta: ${c.card_status} | Cuenta: ${c.account_status}
      </div>`
    ).join("") || "<p>Sin tarjetas</p>";

    const plansDiv = document.getElementById("plansList");
    plansDiv.innerHTML = d.plans.map(p =>
      `<div class="card-item">
        Plan <strong>${p.plan_id}</strong> (${p.merchant}) — Resta: <strong>${p.remaining_mirs} Mirs</strong><br/>
        Plazo: ${p.term_days}d (${p.daily_pct}%/día = ${p.daily_mirs} Mirs/día)
      </div>`
    ).join("");

    document.getElementById("txBody").innerHTML = d.transactions.map(t =>
      `<tr><td>${t.created_at}</td><td>${t.tx_type}</td><td>${t.amount_label}</td><td>${t.card_id || "-"}</td><td>${t.channel}</td><td>${t.note || ""}</td></tr>`
    ).join("");

    if (d.notices && d.notices.length) {
      showToast(d.notices.join(" | "));
    }
  } catch (e) { showToast(e.message, true); }
}

async function createCard() {
  try {
    await api("/cards/create", "POST", {
      account_id: STATE.account_id,
      label: document.getElementById("newCardLabel").value,
      pin: document.getElementById("newCardPin").value
    });
    showToast("Tarjeta creada.");
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function fundCard() {
  try {
    await api("/cards/fund", "POST", {
      account_id: STATE.account_id,
      card_id: document.getElementById("fundCardId").value,
      amount_mirs: parseFloat(document.getElementById("fundAmt").value)
    });
    showToast("Saldo pasado a tarjeta sin PIN.");
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function transferAcc() {
  try {
    const r = await api("/transfers/account", "POST", {
      from_account_id: STATE.account_id,
      to_account_id: document.getElementById("trToAcc").value,
      amount_mirs: parseFloat(document.getElementById("trAmt").value)
    });
    showToast("Transferencia realizada." + (r.absorbed_debt_mirs > 0 ? " Absorbidos por deuda: " + r.absorbed_debt_mirs : ""));
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function payCash() {
  try {
    await api("/pay/cash", "POST", {
      account_id: STATE.account_id,
      card_id: document.getElementById("payCardId").value,
      pin: document.getElementById("payPin").value,
      amount_mirs: parseFloat(document.getElementById("payAmt").value),
      channel: document.getElementById("payChannel").value
    });
    showToast("Pago de contado aprobado.");
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function payLater() {
  try {
    const r = await api("/pay/later/create", "POST", {
      account_id: STATE.account_id,
      card_id: document.getElementById("payCardId").value,
      pin: document.getElementById("payPin").value,
      amount_mirs: parseFloat(document.getElementById("payAmt").value),
      term_days: parseInt(document.getElementById("laterDays").value, 10),
      recipient_account_id: document.getElementById("laterRecAcc").value || null,
      channel: document.getElementById("payChannel").value
    });
    showToast(`Plan ${r.plan_id} activo: ${r.daily_pct}%/día (${r.daily_mirs} Mirs/día)`);
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function simAdvance() {
  try {
    const r = await api("/pay/later/simulate-days", "POST", {
      account_id: STATE.account_id,
      extra_days: parseInt(document.getElementById("simDays").value, 10)
    });
    showToast(r.notices.join(" | ") || "Días simulados.");
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function makeGift() {
  try {
    const r = await api("/gift/create", "POST", {
      account_id: STATE.account_id,
      card_id_for_pin: document.getElementById("giftCardPin").value,
      pin: document.getElementById("giftPin").value,
      amount_mirs: parseFloat(document.getElementById("giftAmt").value)
    });
    document.getElementById("giftOut").textContent = "Código generado: " + r.gift_code;
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function redeemGift() {
  try {
    const r = await api("/gift/redeem", "POST", {
      target_account_id: STATE.account_id,
      gift_code: document.getElementById("redeemCode").value
    });
    showToast(`Canjeados ${r.credited_mirs} Mirs.` + (r.absorbed_debt_mirs > 0 ? ` Absorbidos por deuda: ${r.absorbed_debt_mirs}` : ""));
    await refreshDashboard();
  } catch (e) { showToast(e.message, true); }
}

async function loadCreds() {
  try {
    const cid = document.getElementById("credCardId").value.trim();
    const r = await api(`/cards/${STATE.account_id}/${cid}/credentials`);
    const box = document.getElementById("credsBox");
    box.classList.remove("hidden");
    box.textContent = JSON.stringify(r, null, 2);
  } catch (e) { showToast(e.message, true); }
}

initClient();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/config":
            self._send_json(load_config())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_UI.encode("utf-8"))

    def do_POST(self):
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            save_config(body)
            self._send_json({"ok": True})
            return
        self.send_response(404)
        self.end_headers()

    def _send_json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    url = f"http://127.0.0.1:{LOCAL_PORT}"
    print(f"Interfaz MEBEST corriendo en tu PC en: {url}")
    webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), Handler).serve_forever()
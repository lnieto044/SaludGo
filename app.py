import os, sqlite3, secrets, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime as dt, date, timedelta

# Datos para el tablero (Excel/CSV)
import pandas as pd
import numpy as np

# ------------------------ Config ------------------------

def get_db_path():
    return os.environ.get(
        "DATABASE_PATH",
        os.path.join(os.path.dirname(__file__), "saludgo.db")
    )

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-saludgo")
app.config["DB_PATH"] = get_db_path()

# Rutas de datos
app.config["REPORTS_XLSX_PATH"] = os.environ.get(
    "REPORTS_XLSX_PATH",
    os.path.join(os.path.dirname(__file__), "data", "Proyeccciones_salud_limpia.xlsx")
)
app.config["PRECIP_CSV_PATH"] = os.environ.get(
    "PRECIP_CSV_PATH",
    os.path.join(os.path.dirname(__file__), "data", "Precipitaciones_Totales_Mensuales_20250926.csv")
)

# Límite de citas por día
MAX_APPOINTMENTS_PER_DAY = int(os.environ.get("MAX_APPOINTMENTS_PER_DAY", 10))

# Correo del administrador (por defecto el que me diste)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "guillermonieto.200391@gmail.com")

# ------------------------ Email helper ------------------------

def send_email(to_email, subject, html_body, text_body=None):
    """
    Envía un correo usando variables de entorno SMTP_*.
    Si no hay configuración, solo imprime en consola (útil para desarrollo).
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("SMTP_FROM", user or "no-reply@saludgo.local")

    # Sin configuración SMTP: log a consola
    if not host or not user or not password:
        print("=== EMAIL (simulado) ===")
        print("Para:", to_email)
        print("Asunto:", subject)
        print(html_body)
        print("=== FIN EMAIL ===")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)

# ------------------------ DB helpers ------------------------

def to_int(x, default=None):
    """Convierte a int si es posible; si no, default."""
    try:
        if x is None or str(x).strip() == "":
            return default
        return int(float(str(x).replace(",", ".").strip()))
    except Exception:
        return default

def to_float(x, default=None):
    """Convierte a float si es posible; si no, default."""
    try:
        if x is None or str(x).strip() == "":
            return default
        return float(str(x).replace(",", ".").strip())
    except Exception:
        return default

def empty_to_none(s):
    """Devuelve None si viene vacío/espacios; si no, el string limpio."""
    s = (s or "").strip()
    return s if s else None

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  email TEXT,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  depto TEXT, municipio TEXT, name TEXT NOT NULL, type TEXT,
  lat REAL, lon REAL, capacity INTEGER DEFAULT 30, available INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, type TEXT, fecha TEXT,
  cod_depto TEXT, cod_mpio TEXT, depto TEXT, municipio TEXT,
  expected_patients INTEGER, target TEXT, resources TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS community_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fecha TEXT, cod_depto TEXT, cod_mpio TEXT, depto TEXT, municipio TEXT,
  symptom TEXT, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS availability_slots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT, fecha TEXT, horario TEXT,
  cod_depto TEXT, cod_mpio TEXT, depto TEXT, municipio TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_resets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  fecha TEXT,
  hora TEXT,
  tipo TEXT,
  lugar TEXT,
  motivo TEXT,
  estado TEXT DEFAULT 'Agendada',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS meds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  medicamento TEXT,
  dosis TEXT,
  frecuencia TEXT,
  fecha_entrega TEXT,
  lugar TEXT,
  estado TEXT DEFAULT 'pendiente',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  sender TEXT NOT NULL, -- 'user' o 'bot'
  message TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
);
"""

def init_db():
    db = get_db()
    db.executescript(SCHEMA_SQL)
    c = db.cursor()
    # admin por defecto
    if not c.execute("SELECT 1 FROM users WHERE username=?", ("admin",)).fetchone():
        c.execute(
            "INSERT INTO users(username,email,password_hash,role) VALUES(?,?,?,?)",
            ("admin", "admin@saludgo.local", generate_password_hash("admin123"), "admin"),
        )
    # servicios demo
    if c.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
        demo = [
            ("Antioquia", "Medellín", "Hospital Central", "hospital", 6.2442, -75.5812, 120, 1),
            ("Antioquia", "Bello", "Puesto La Cumbre", "puesto", 6.335, -75.558, 40, 1),
            ("Cundinamarca", "Bogotá", "Móvil #1", "móvil", 4.711, -74.072, 25, 1),
        ]
        c.executemany(
            """INSERT INTO services(depto,municipio,name,type,lat,lon,capacity,available)
               VALUES(?,?,?,?,?,?,?,?)""",
            demo,
        )
    db.commit()

@app.before_request
def ensure_db():
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db()

# ------------------------ Auth helpers ------------------------

def current_user():
    if "user_id" in session:
        return get_db().execute(
            "SELECT * FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
    return None

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            flash("Debes ingresar para ver esta página.", "warning")
            return redirect(url_for("index", show_login=1))
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u or u["role"] != "admin":
            flash("Acceso solo para administradores", "danger")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped

# ------------------------ Rutas públicas ------------------------

POSTS = [
    {
        "title": "Brigada de salud en zona rural de Cundinamarca",
        "img": "https://res.cloudinary.com/academia-geek-1/image/upload/v1759087228/ChatGPT_Image_28_sept_2025_14_15_07_f15myp.png",
        "excerpt": "Más de 600 personas recibieron valoración, vacunación y educación en autocuidado...",
        "date": "15 Dic, 2025",
        "tags": ["Promoción", "Extramural"],
        "url": "#",
    },
    {
        "title": "Avances del plan de vacunación 2025",
        "img": "https://res.cloudinary.com/academia-geek-1/image/upload/v1759087228/ChatGPT_Image_28_sept_2025_13_46_43_r3icx0.png",
        "excerpt": "Reportamos coberturas superiores al 95% en biológicos del PAI gracias a rutas móviles...",
        "date": "02 Dic, 2025",
        "tags": ["Vacunación", "Coberturas"],
        "url": "#",
    },
    {
        "title": "Atención centrada en las personas",
        "img": "https://res.cloudinary.com/academia-geek-1/image/upload/v1759087227/ChatGPT_Image_28_sept_2025_14_14_44_vqy4pe.png",
        "excerpt": "Implementamos rondas de satisfacción y protocolos de comunicación...",
        "date": "22 Nov, 2025",
        "tags": ["Calidad", "Humanización"],
        "url": "#",
    },
]

@app.route("/")
def index():
    db = get_db()
    campaigns = db.execute(
        """
        SELECT name, type, fecha, depto, municipio, expected_patients
        FROM campaigns
        WHERE fecha IS NOT NULL
        ORDER BY fecha ASC
        LIMIT 6
        """
    ).fetchall()

    return render_template(
        "index.html",
        user=current_user(),
        posts=POSTS,
        campaigns=campaigns,
    )

@app.route("/participa", methods=["GET", "POST"])
def participa():
    db = get_db()
    message = None

    if request.method == "POST":
        form_type = request.form.get("form_type", "").strip()

        # Anti-spam (honeypot)
        if (request.form.get("website") or request.form.get("homepage")):
            flash("No se pudo guardar el envío.", "danger")
            return redirect(url_for("participa"))

        def parse_date(s):
            try:
                return dt.strptime(s, "%Y-%m-%d").date()
            except Exception:
                return None

        if form_type == "symptom":
            fecha = parse_date(request.form.get("fecha", ""))
            depto = (request.form.get("depto") or "").strip()
            municipio = (request.form.get("municipio") or "").strip()
            symptom = (request.form.get("symptom") or "").strip()
            details = (request.form.get("details") or "").strip()

            if not (fecha and depto and municipio and symptom):
                flash("Faltan datos en el reporte de síntomas.", "warning")
                return redirect(url_for("participa"))

            try:
                db.execute(
                    """INSERT INTO community_reports(fecha,depto,municipio,symptom,details)
                       VALUES(?,?,?,?,?)""",
                    (fecha.isoformat(), depto, municipio, symptom, details),
                )
                db.commit()
                message = "Gracias. Tu reporte fue registrado."
            except Exception as e:
                flash(f"No se pudo guardar el reporte: {e}", "danger")
                return redirect(url_for("participa"))

        elif form_type == "avail":
            fecha = parse_date(request.form.get("fecha", ""))
            username = (request.form.get("username") or "").strip() or None
            horario = (request.form.get("horario") or "").strip()
            depto = (request.form.get("depto") or "Cundinamarca").strip()
            municipio = (request.form.get("municipio") or "").strip()

            if not (fecha and horario and depto and municipio):
                flash("Faltan datos en la disponibilidad.", "warning")
                return redirect(url_for("participa"))

            try:
                db.execute(
                    """INSERT INTO availability_slots(username,fecha,horario,depto,municipio)
                       VALUES(?,?,?,?,?)""",
                    (username, fecha.isoformat(), horario, depto, municipio),
                )
                db.commit()
                message = "Disponibilidad registrada."
            except Exception as e:
                flash(f"No se pudo registrar la disponibilidad: {e}", "danger")
                return redirect(url_for("participa"))

        else:
            flash("Formulario no reconocido.", "danger")
            return redirect(url_for("participa"))

    return render_template("participa.html", user=current_user(), message=message)

# ------------------------ Auth ------------------------

@app.post("/auth/login")
def auth_login():
    db = get_db()
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "")
    row = db.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    if row and check_password_hash(row["password_hash"], p):
        session["user_id"] = row["id"]
        flash(f"Bienvenido, {row['username']}", "success")

        next_url = request.form.get("next") or None
        if next_url:
            return redirect(next_url)

        if row["role"] == "admin":
            return redirect(url_for("admin"))
        else:
            return redirect(url_for("perfil"))

    flash("Usuario o contraseña incorrectos", "danger")
    return redirect(url_for("index", show_login=1, tab="login"))

@app.post("/auth/register")
def auth_register():
    db = get_db()
    u = request.form.get("username", "").strip()
    email = request.form.get("email") or None
    p = request.form.get("password", "")
    if not u or not p:
        flash("Usuario y contraseña son obligatorios", "warning")
        return redirect(url_for("index", show_login=1, tab="signup"))
    try:
        db.execute(
            "INSERT INTO users(username,email,password_hash,role) VALUES(?,?,?,?)",
            (u, email, generate_password_hash(p), "user"),
        )
        db.commit()
        flash("Cuenta creada. Ya puedes ingresar.", "success")
    except Exception as e:
        flash(f"No se pudo crear la cuenta: {e}", "danger")
    return redirect(url_for("index", show_login=1, tab="login"))

@app.post("/auth/forgot")
def auth_forgot():
    db = get_db()
    identifier = (request.form.get("identifier") or "").strip()

    if not identifier:
        flash("Ingresa tu usuario o correo para recuperar tu contraseña.", "warning")
        return redirect(url_for("index", show_login=1, tab="forgot"))

    user = db.execute(
        "SELECT id, username, email FROM users WHERE username=? OR email=?",
        (identifier, identifier),
    ).fetchone()

    msg_generic = "Si encontramos una cuenta asociada, te enviaremos un enlace para recuperar tu contraseña."

    if not user:
        flash(msg_generic, "info")
        return redirect(url_for("index", show_login=1, tab="forgot"))

    if not user["email"]:
        flash("Tu usuario no tiene un correo registrado. Contacta al administrador.", "warning")
        return redirect(url_for("index", show_login=1, tab="forgot"))

    token = secrets.token_urlsafe(32)
    created_at = dt.utcnow().isoformat()

    db.execute(
        "INSERT INTO password_resets(user_id, token, created_at, used) VALUES(?,?,?,0)",
        (user["id"], token, created_at),
    )
    db.commit()

    reset_url = url_for("reset_password", token=token, _external=True)

    subject = "Recuperación de contraseña — SaludGo"
    text_body = (
        f"Hola {user['username']},\n\n"
        "Recibimos una solicitud para restablecer tu contraseña en SaludGo.\n\n"
        f"Usa el siguiente enlace (válido por 5 minutos):\n{reset_url}\n\n"
        "Si tú no hiciste esta solicitud, puedes ignorar este mensaje.\n"
    )
    html_body = f"""
    <html>
      <body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#f5f7fb;padding:24px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;">
          <tr>
            <td style="padding:20px 24px;background:#0f766e;color:#ffffff;">
              <h2 style="margin:0;font-size:20px;">SaludGo</h2>
              <p style="margin:4px 0 0;font-size:13px;opacity:.9;">Recuperación de contraseña</p>
            </td>
          </tr>
          <tr>
            <td style="padding:24px;">
              <p style="font-size:15px;margin:0 0 12px;">Hola <strong>{user['username']}</strong>,</p>
              <p style="font-size:14px;margin:0 0 12px;">
                Hemos recibido una solicitud para restablecer tu contraseña en <strong>SaludGo</strong>.
              </p>
              <p style="font-size:14px;margin:0 0 16px;">
                Haz clic en el siguiente botón para crear una nueva contraseña. Este enlace será válido durante
                <strong>5 minutos</strong>. Pasado ese tiempo, deberás solicitar un nuevo enlace.
              </p>
              <p style="text-align:center;margin:24px 0;">
                <a href="{reset_url}"
                   style="display:inline-block;padding:10px 24px;border-radius:999px;background:#16a34a;color:#ffffff;
                          text-decoration:none;font-weight:600;font-size:14px;">
                  Cambiar mi contraseña
                </a>
              </p>
              <p style="font-size:12px;color:#6b7280;margin:0 0 4px;">
                Si el botón no funciona, copia y pega este enlace en tu navegador:
              </p>
              <p style="font-size:12px;color:#4b5563;word-break:break-all;margin:0 0 16px;">
                {reset_url}
              </p>
              <p style="font-size:12px;color:#6b7280;margin:0;">
                Si tú no solicitaste este cambio, puedes ignorar este mensaje. Tu contraseña actual seguirá siendo válida.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px;background:#f3f4f6;font-size:11px;color:#6b7280;text-align:center;">
              MVP SaludGo · Este correo se generó automáticamente, por favor no respondas a este mensaje.
            </td>
          </tr>
        </table>
      </body>
    </html>
    """

    send_email(user["email"], subject, html_body, text_body)
    flash(msg_generic, "info")
    return redirect(url_for("index", show_login=1, tab="login"))

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    row = db.execute(
        """SELECT pr.id, pr.user_id, pr.token, pr.created_at, pr.used, u.username
           FROM password_resets pr
           JOIN users u ON pr.user_id = u.id
           WHERE pr.token=?""",
        (token,),
    ).fetchone()

    if not row:
        flash("El enlace de recuperación no es válido. Solicita uno nuevo.", "danger")
        return redirect(url_for("index", show_login=1, tab="forgot"))

    try:
        created_at = dt.fromisoformat(row["created_at"])
    except Exception:
        created_at = None

    expired = not created_at or (dt.utcnow() - created_at > timedelta(minutes=5))

    if row["used"] or expired:
        flash("Este enlace de recuperación ha expirado. Solicita uno nuevo.", "warning")
        return redirect(url_for("index", show_login=1, tab="forgot"))

    if request.method == "POST":
        p1 = request.form.get("password") or ""
        p2 = request.form.get("confirm") or ""

        if len(p1) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "warning")
            return redirect(url_for("reset_password", token=token))

        if p1 != p2:
            flash("Las contraseñas no coinciden.", "warning")
            return redirect(url_for("reset_password", token=token))

        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(p1), row["user_id"]),
        )
        db.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
        db.commit()

        flash("Tu contraseña fue actualizada. Ya puedes ingresar con tu nuevo acceso.", "success")
        return redirect(url_for("index", show_login=1, tab="login"))

    return render_template(
        "reset_password.html",
        username=row["username"],
        token=token,
        user=current_user(),
    )

@app.get("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))

# ------------------------ Admin principal ------------------------

@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    services = db.execute("SELECT * FROM services ORDER BY created_at DESC").fetchall()
    campaigns = db.execute(
        "SELECT * FROM campaigns ORDER BY (fecha IS NULL), fecha DESC, created_at DESC"
    ).fetchall()
    users = db.execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY created_at DESC"
    ).fetchall()

    reports = db.execute(
        """
        SELECT id, fecha, depto, municipio, symptom,
               substr(coalesce(details,''),1,80) AS details, created_at
        FROM community_reports
        ORDER BY created_at DESC
        LIMIT 20
    """
    ).fetchall()

    slots = db.execute(
        """
        SELECT id, coalesce(username,'(Anónimo)') as username,
               fecha, horario, depto, municipio, created_at
        FROM availability_slots
        ORDER BY created_at DESC
        LIMIT 20
    """
    ).fetchall()

    return render_template(
        "admin.html",
        user=current_user(),
        services=services,
        campaigns=campaigns,
        users=users,
        reports=reports,
        slots=slots,
    )


# ----- CRUD servicios/campañas/usuarios -----

@app.post("/admin/services/create")
@admin_required
def admin_services_create():
    f = request.form
    db = get_db()
    db.execute(
        """INSERT INTO services(depto,municipio,name,type,lat,lon,capacity,available)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            f.get("depto"),
            f.get("municipio"),
            f.get("name"),
            f.get("type"),
            float(f.get("lat")) if f.get("lat") else None,
            float(f.get("lon")) if f.get("lon") else None,
            int(f.get("capacity") or 30),
            1 if f.get("available") == "on" else 0,
        ),
    )
    db.commit()
    flash("Servicio creado", "success")
    return redirect(url_for("admin"))

@app.post("/admin/campaigns/create")
@admin_required
def admin_campaigns_create():
    f = request.form
    db = get_db()
    db.execute(
        """INSERT INTO campaigns(name,type,fecha,depto,municipio,expected_patients,target,resources)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            f.get("name"),
            f.get("type"),
            f.get("fecha"),
            f.get("depto"),
            f.get("municipio"),
            int(f.get("expected_patients") or 0),
            f.get("target"),
            f.get("resources"),
        ),
    )
    db.commit()
    flash("Campaña creada", "success")
    return redirect(url_for("admin"))

@app.post("/admin/users/role")
@admin_required
def admin_users_role():
    db = get_db()
    user_id = request.form.get("user_id")
    role = request.form.get("role")
    if role not in ("admin", "user"):
        flash("Rol inválido", "danger")
    else:
        db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        db.commit()
        flash("Rol actualizado", "success")
    return redirect(url_for("admin"))

@app.post("/admin/delete")
@admin_required
def admin_delete():
    db = get_db()
    what = request.form.get("what")
    _id = request.form.get("id")
    if what == "service":
        db.execute("DELETE FROM services WHERE id=?", (_id,))
    elif what == "campaign":
        db.execute("DELETE FROM campaigns WHERE id=?", (_id,))
    elif what == "user":
        if str(current_user()["id"]) == str(_id):
            flash("No puedes borrarte a ti mismo.", "warning")
            return redirect(url_for("admin"))
        db.execute("DELETE FROM users WHERE id=?", (_id,))
    else:
        flash("Operación no válida", "danger")
        return redirect(url_for("admin"))
    db.commit()
    flash("Eliminado", "info")
    return redirect(url_for("admin"))

@app.post("/admin/services/update")
@admin_required
def admin_services_update():
    db = get_db()
    f = request.form

    svc_id = f.get("id")
    if not svc_id:
        flash("Falta el ID del servicio.", "danger")
        return redirect(url_for("admin"))

    try:
        db.execute(
            """
            UPDATE services
               SET name=?, type=?, depto=?, municipio=?,
                   lat=?, lon=?, capacity=?, available=?
             WHERE id=?
        """,
            (
                empty_to_none(f.get("name")),
                empty_to_none(f.get("type")),
                empty_to_none(f.get("depto")),
                empty_to_none(f.get("municipio")),
                to_float(f.get("lat")),
                to_float(f.get("lon")),
                to_int(f.get("capacity"), 0),
                1 if f.get("available") == "on" else 0,
                svc_id,
            ),
        )
        db.commit()
        flash("Servicio actualizado.", "success")
    except Exception as e:
        flash(f"No se pudo actualizar el servicio: {e}", "danger")

    return redirect(url_for("admin"))

@app.post("/admin/campaigns/update")
@admin_required
def admin_campaigns_update():
    db = get_db()
    f = request.form

    cmp_id = f.get("id")
    if not cmp_id:
        flash("Falta el ID de la campaña.", "danger")
        return redirect(url_for("admin"))

    try:
        db.execute(
            """
            UPDATE campaigns
               SET name=?, type=?, fecha=?, depto=?, municipio=?,
                   expected_patients=?, target=?, resources=?
             WHERE id=?
        """,
            (
                empty_to_none(f.get("name")),
                empty_to_none(f.get("type")),
                empty_to_none(f.get("fecha")),
                empty_to_none(f.get("depto")),
                empty_to_none(f.get("municipio")),
                to_int(f.get("expected_patients"), 0),
                empty_to_none(f.get("target")),
                empty_to_none(f.get("resources")),
                cmp_id,
            ),
        )
        db.commit()
        flash("Campaña actualizada.", "success")
    except Exception as e:
        flash(f"No se pudo actualizar la campaña: {e}", "danger")

    return redirect(url_for("admin"))

@app.post("/admin/users/update")
@admin_required
def admin_users_update():
    f = request.form
    uid = f.get("id")
    if not uid:
        flash("Falta el ID del usuario.", "danger")
        return redirect(url_for("admin"))

    username = (f.get("username") or "").strip()
    email = f.get("email") or None

    if not username:
        flash("El usuario no puede quedar vacío.", "warning")
        return redirect(url_for("admin"))

    db = get_db()
    try:
        exists = db.execute(
            "SELECT id FROM users WHERE username=? AND id<>?", (username, uid)
        ).fetchone()
        if exists:
            flash("Ya existe un usuario con ese nombre.", "warning")
            return redirect(url_for("admin"))

        db.execute(
            "UPDATE users SET username=?, email=? WHERE id=?", (username, email, uid)
        )
        db.commit()
        flash("Usuario actualizado.", "success")
    except Exception as e:
        flash(f"No se pudo actualizar el usuario: {e}", "danger")

    return redirect(url_for("admin"))

# ------------------------ Perfil ------------------------

@app.route("/perfil")
@login_required
def perfil():
    u = current_user()
    db = get_db()

    stats = None
    if u["role"] == "admin":
        stats = {
            "services":   db.execute("SELECT COUNT(*) FROM services").fetchone()[0],
            "campaigns":  db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0],
            "reports":    db.execute("SELECT COUNT(*) FROM community_reports").fetchone()[0],
            "slots":      db.execute("SELECT COUNT(*) FROM availability_slots").fetchone()[0],
            "users":      db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        }

    my_slots = db.execute("""
        SELECT fecha, horario, depto, municipio
        FROM availability_slots
        WHERE username = ?
        ORDER BY created_at DESC
        LIMIT 5
    """, (u["username"],)).fetchall()

    my_appointments = db.execute(
        """SELECT fecha, hora, tipo, lugar, estado
           FROM appointments
           WHERE user_id=?
           ORDER BY fecha, hora""",
        (u["id"],),
    ).fetchall()

    my_meds = db.execute(
        """SELECT medicamento, dosis, frecuencia, fecha_entrega, lugar, estado
           FROM meds
           WHERE user_id=?
           ORDER BY fecha_entrega""",
        (u["id"],),
    ).fetchall()

    return render_template(
        "perfil.html",
        user=u,
        stats=stats,
        my_slots=my_slots,
        my_appointments=my_appointments,
        my_meds=my_meds,
    )

@app.post("/perfil/update")
@login_required
def perfil_update():
    u = current_user()
    db = get_db()

    username = (request.form.get("username") or "").strip()
    email    = (request.form.get("email") or "").strip() or None

    if not username:
        flash("El usuario no puede quedar vacío.", "warning")
        return redirect(url_for("perfil"))

    exists = db.execute(
        "SELECT id FROM users WHERE username=? AND id<>?",
        (username, u["id"])
    ).fetchone()
    if exists:
        flash("Ya existe un usuario con ese nombre.", "warning")
        return redirect(url_for("perfil"))

    db.execute(
        "UPDATE users SET username=?, email=? WHERE id=?",
        (username, email, u["id"])
    )
    db.commit()
    flash("Datos de cuenta actualizados.", "success")
    return redirect(url_for("perfil"))

# ------------------------ Citas: vista usuario ------------------------

@app.route("/mis_citas", methods=["GET", "POST"])
@login_required
def mis_citas():
    u = current_user()
    db = get_db()
    MAX_PER_DAY = MAX_APPOINTMENTS_PER_DAY

    def parse_date(s):
        try:
            return dt.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    if request.method == "POST":
        tipo = (request.form.get("tipo") or "").strip() or "Consulta"
        motivo = (request.form.get("motivo") or "").strip()
        fecha_str = request.form.get("preferencia_fecha") or ""
        hora_str = request.form.get("preferencia_hora") or ""
        lugar = (request.form.get("lugar") or "").strip() or "Por definir"
        fecha = parse_date(fecha_str)

        if not fecha:
            flash("Selecciona una fecha válida para la cita.", "warning")
            return redirect(url_for("mis_citas"))

        if fecha < date.today():
            flash("No puedes agendar en una fecha pasada.", "warning")
            return redirect(url_for("mis_citas"))

        total = db.execute(
            "SELECT COUNT(*) FROM appointments WHERE fecha=?",
            (fecha.isoformat(),),
        ).fetchone()[0]

        if total >= MAX_PER_DAY:
            flash("Esa fecha ya no tiene cupos disponibles. Elige otro día.", "warning")
            return redirect(url_for("mis_citas"))

        estado = "Agendada"

        db.execute(
            """INSERT INTO appointments(user_id, fecha, hora, tipo, lugar, motivo, estado)
               VALUES(?,?,?,?,?,?,?)""",
            (u["id"], fecha.isoformat(), hora_str or None, tipo, lugar, motivo, estado),
        )
        db.commit()

        flash("Tu cita se generó exitosamente.", "success")

        if u["email"]:
            subject = "Cita agendada — SaludGo"
            text_body = (
                f"Hola {u['username']},\n\n"
                f"Hemos registrado tu cita de {tipo} para el {fecha.isoformat()} "
                f"a las {hora_str or 'por definir'}.\n\n"
                f"Lugar: {lugar}\n"
                f"Estado: {estado}\n"
                f"Motivo: {motivo or '-'}\n"
            )
            html_body = f"""
            <html><body style="font-family:system-ui">
              <p>Hola <strong>{u['username']}</strong>,</p>
              <p>Hemos registrado tu cita de <strong>{tipo}</strong> para el
                 <strong>{fecha.isoformat()}</strong> a las
                 <strong>{hora_str or 'por definir'}</strong>.</p>
              <p>
                <strong>Lugar:</strong> {lugar}<br>
                <strong>Estado:</strong> {estado}<br>
                <strong>Motivo:</strong> {motivo or '-'}
              </p>
              <p>Gracias por usar SaludGo.</p>
            </body></html>
            """
            send_email(u["email"], subject, html_body, text_body)

        if ADMIN_EMAIL:
            txt_admin = (
                f"Nuevo agendamiento de cita\n\n"
                f"Usuario: {u['username']}\n"
                f"Fecha: {fecha.isoformat()}\n"
                f"Hora: {hora_str or 'por definir'}\n"
                f"Tipo: {tipo}\n"
                f"Lugar: {lugar}\n"
                f"Motivo: {motivo or '-'}\n"
                f"Estado: {estado}\n"
            )
            send_email(
                ADMIN_EMAIL,
                "Nueva cita programada — SaludGo",
                f"<pre>{txt_admin}</pre>",
                txt_admin,
            )

        return redirect(url_for("mis_citas"))

    my_appointments = db.execute(
        """SELECT fecha, hora, tipo, lugar, estado
           FROM appointments
           WHERE user_id=?
           ORDER BY fecha, hora""",
        (u["id"],),
    ).fetchall()

    today = date.today()
    horizon = today + timedelta(days=30)

    rows = db.execute(
        """SELECT fecha, COUNT(*) as n
           FROM appointments
           WHERE fecha BETWEEN ? AND ?
           GROUP BY fecha""",
        (today.isoformat(), horizon.isoformat()),
    ).fetchall()

    disabled_dates = [r["fecha"] for r in rows if r["n"] >= MAX_PER_DAY]

    return render_template(
        "mis_citas.html",
        user=u,
        my_appointments=my_appointments,
        disabled_dates=disabled_dates,
        today=today.isoformat(),
        max_date=horizon.isoformat(),
        max_per_day=MAX_PER_DAY,
    )

# ----- Crear cita desde el admin -----

@app.post("/admin/appointments/create")
@admin_required
def admin_appointments_create():
    db = get_db()
    f = request.form

    user_id = f.get("user_id")
    fecha = (f.get("fecha") or "").strip() or None
    hora = (f.get("hora") or "").strip() or None
    tipo = (f.get("tipo") or "").strip()
    lugar = (f.get("lugar") or "Por definir").strip()
    note = (f.get("motivo") or "").strip()
    estado = (f.get("estado") or "Agendada").strip()

    u = db.execute(
        "SELECT id, username, email FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not u:
        flash("Usuario inválido para la cita.", "danger")
        return redirect(url_for("admin"))

    db.execute(
        """
        INSERT INTO appointments(user_id,fecha,hora,tipo,lugar,note,estado)
        VALUES(?,?,?,?,?,?,?)
        """,
        (u["id"], fecha, hora, tipo, lugar, note, estado),
    )
    db.commit()

    # correo al usuario
    if u["email"]:
        subject = "Cita programada — SaludGo"
        html = f"""
        <p>Hola <b>{u['username']}</b>,</p>
        <p>Se te ha programado una cita.</p>
        <ul>
          <li><b>Fecha:</b> {fecha or 'Por asignar'}</li>
          <li><b>Hora:</b> {hora or 'Por asignar'}</li>
          <li><b>Tipo:</b> {tipo or '—'}</li>
          <li><b>Lugar:</b> {lugar or 'Por definir'}</li>
          <li><b>Estado:</b> {estado}</li>
        </ul>
        <p>Por favor conserva este mensaje.</p>
        """
        send_email(u["email"], subject, html)

    # copia al admin
    admin_email = app.config.get("ADMIN_EMAIL")
    if admin_email:
        send_email(
            admin_email,
            "Nueva cita creada — SaludGo",
            f"""
            <p>Se creó una nueva cita:</p>
            <ul>
              <li><b>Usuario:</b> {u['username']} (id {u['id']})</li>
              <li><b>Fecha:</b> {fecha or 'Por asignar'}</li>
              <li><b>Hora:</b> {hora or 'Por asignar'}</li>
              <li><b>Tipo:</b> {tipo or '—'}</li>
              <li><b>Lugar:</b> {lugar or 'Por definir'}</li>
              <li><b>Estado:</b> {estado}</li>
            </ul>
            """,
        )

    flash("Cita creada exitosamente.", "success")
    return redirect(url_for("admin"))

# ----- Registrar medicamento desde el admin -----

@app.post("/admin/medications/create")
@admin_required
def admin_medications_create():
    db = get_db()
    f = request.form

    user_id = f.get("user_id")
    med = (f.get("medicamento") or "").strip()
    dosis = (f.get("dosis") or "").strip()
    freq = (f.get("frecuencia") or "").strip()
    fecha = (f.get("fecha_entrega") or "").strip() or None
    lugar = (f.get("lugar") or "").strip()
    estado = (f.get("estado") or "pendiente").strip()

    if not (user_id and med):
        flash("Falta usuario o medicamento.", "warning")
        return redirect(url_for("admin"))

    u = db.execute(
        "SELECT id, username, email FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not u:
        flash("Usuario inválido.", "danger")
        return redirect(url_for("admin"))

    db.execute(
        """
        INSERT INTO medications(
            user_id, medicamento, dosis, frecuencia, fecha_entrega, lugar, estado
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (u["id"], med, dosis, freq, fecha, lugar, estado),
    )
    db.commit()

    if u["email"]:
        send_email(
            u["email"],
            "Asignación de medicamento — SaludGo",
            f"""
            <p>Hola <b>{u['username']}</b>,</p>
            <p>Se ha registrado el siguiente medicamento:</p>
            <ul>
              <li><b>Medicamento:</b> {med}</li>
              <li><b>Dosis:</b> {dosis or '—'}</li>
              <li><b>Frecuencia:</b> {freq or '—'}</li>
              <li><b>Próxima entrega:</b> {fecha or '—'}</li>
              <li><b>Lugar:</b> {lugar or '—'}</li>
              <li><b>Estado:</b> {estado}</li>
            </ul>
            """,
        )

    flash("Medicamento registrado.", "success")
    return redirect(url_for("admin"))


# ------------------------ Admin: citas y medicamentos ------------------------

@app.route("/admin/citas", methods=["GET", "POST"])
@admin_required
def admin_citas():
    db = get_db()
    users = db.execute(
        "SELECT id, username, email FROM users ORDER BY username"
    ).fetchall()

    if request.method == "POST":
        user_id = request.form.get("user_id")
        fecha = (request.form.get("fecha") or "").strip() or None
        hora = (request.form.get("hora") or "").strip() or None
        tipo = (request.form.get("tipo") or "").strip() or "Consulta"
        lugar = (request.form.get("lugar") or "").strip() or "Por definir"
        motivo = (request.form.get("motivo") or "").strip()
        estado = (request.form.get("estado") or "").strip() or "Agendada"

        if not user_id:
            flash("Debes seleccionar un usuario.", "warning")
            return redirect(url_for("admin_citas"))

        db.execute(
            """INSERT INTO appointments(user_id, fecha, hora, tipo, lugar, motivo, estado)
               VALUES(?,?,?,?,?,?,?)""",
            (user_id, fecha, hora, tipo, lugar, motivo, estado),
        )
        db.commit()

        user = db.execute(
            "SELECT username, email FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if user and user["email"]:
            subject = "Cita creada por el equipo de SaludGo"
            text_body = (
                f"Hola {user['username']},\n\n"
                f"Hemos registrado una cita de {tipo} para el {fecha or 'por definir'} "
                f"a las {hora or 'por definir'}.\n"
                f"Lugar: {lugar}\nEstado: {estado}\nMotivo: {motivo or '-'}\n\n"
                "Si no reconoces esta cita, por favor comunícate con nosotros."
            )
            html_body = f"""
            <html><body style="font-family:system-ui">
              <p>Hola <strong>{user['username']}</strong>,</p>
              <p>Hemos registrado una cita de <strong>{tipo}</strong> para el
                 <strong>{fecha or 'por definir'}</strong> a las
                 <strong>{hora or 'por definir'}</strong>.</p>
              <p>
                <strong>Lugar:</strong> {lugar}<br>
                <strong>Estado:</strong> {estado}<br>
                <strong>Motivo:</strong> {motivo or '-'}
              </p>
              <p>Si no reconoces esta cita, por favor comunícate con nosotros.</p>
            </body></html>
            """
            send_email(user["email"], subject, html_body, text_body)

        flash("Cita creada para el usuario.", "success")
        return redirect(url_for("admin_citas"))

    appointments = db.execute(
        """SELECT a.id, a.fecha, a.hora, a.tipo, a.lugar, a.estado, a.motivo,
                  u.username
           FROM appointments a
           JOIN users u ON a.user_id = u.id
           ORDER BY a.created_at DESC
           LIMIT 100"""
    ).fetchall()

    return render_template(
        "admin_citas.html",
        user=current_user(),
        appointments=appointments,
        users=users,
    )

@app.route("/admin/medicamentos", methods=["GET", "POST"])
@admin_required
def admin_medicamentos():
    db = get_db()
    users = db.execute(
        "SELECT id, username, email FROM users ORDER BY username"
    ).fetchall()

    if request.method == "POST":
        user_id = request.form.get("user_id")
        med = (request.form.get("medicamento") or "").strip()
        dosis = (request.form.get("dosis") or "").strip()
        frecuencia = (request.form.get("frecuencia") or "").strip()
        fecha_entrega = (request.form.get("fecha_entrega") or "").strip() or None
        lugar = (request.form.get("lugar") or "").strip()
        estado = (request.form.get("estado") or "").strip() or "pendiente"

        if not user_id or not med:
            flash("Selecciona un usuario y escribe el medicamento.", "warning")
            return redirect(url_for("admin_medicamentos"))

        db.execute(
            """INSERT INTO meds(user_id, medicamento, dosis, frecuencia, fecha_entrega, lugar, estado)
               VALUES(?,?,?,?,?,?,?)""",
            (user_id, med, dosis, frecuencia, fecha_entrega, lugar, estado),
        )
        db.commit()

        user = db.execute(
            "SELECT username, email FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user and user["email"]:
            subject = "Actualización de medicamentos — SaludGo"
            text_body = (
                f"Hola {user['username']},\n\n"
                f"Hemos registrado el medicamento {med}.\n"
                f"Dosis: {dosis or '-'}\nFrecuencia: {frecuencia or '-'}\n"
                f"Próxima entrega: {fecha_entrega or 'por definir'}\n"
                f"Lugar: {lugar or 'por definir'}\nEstado: {estado}\n"
            )
            html_body = f"""
            <html><body style="font-family:system-ui">
              <p>Hola <strong>{user['username']}</strong>,</p>
              <p>Hemos registrado el medicamento <strong>{med}</strong>.</p>
              <p>
                <strong>Dosis:</strong> {dosis or '-'}<br>
                <strong>Frecuencia:</strong> {frecuencia or '-'}<br>
                <strong>Próxima entrega:</strong> {fecha_entrega or 'por definir'}<br>
                <strong>Lugar:</strong> {lugar or 'por definir'}<br>
                <strong>Estado:</strong> {estado}
              </p>
            </body></html>
            """
            send_email(user["email"], subject, html_body, text_body)

        flash("Medicamento registrado para el usuario.", "success")
        return redirect(url_for("admin_medicamentos"))

    meds = db.execute(
        """SELECT m.id, m.medicamento, m.dosis, m.frecuencia, m.fecha_entrega, m.lugar, m.estado,
                  u.username
           FROM meds m
           JOIN users u ON m.user_id = u.id
           ORDER BY m.created_at DESC
           LIMIT 100"""
    ).fetchall()

    return render_template(
        "admin_medicamentos.html",
        user=current_user(),
        meds=meds,
        users=users,
    )

# ------------------------ API (dashboard Excel/CSV) ------------------------

AREAS_VALIDAS = [
    "Cabecera Municipal",
    "Centros Poblados y Rural Disperso",
    "Total",
]

def _safe_int(x):
    try:
        return int(float(str(x).replace(",", ".").strip()))
    except Exception:
        return 0

def _precipitacion_promedio(muni=None):
    pth = app.config["PRECIP_CSV_PATH"]
    meses = [
        "ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
        "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE",
    ]
    if not os.path.exists(pth):
        return {"meses": meses, "valores": [0] * 12}
    df = pd.read_csv(pth)
    if muni:
        dff = df[df["MUNICIPIO"].astype(str).str.contains(muni, case=False, na=False)]
        if not dff.empty:
            df = dff
    vals = []
    for m in meses:
        v = pd.to_numeric(df[m], errors="coerce")
        vals.append(round(float(np.nanmean(v)), 1) if v.notna().any() else 0.0)
    return {"meses": meses, "valores": vals}

def load_excel_summary(areas=None, municipio_precip=None):
    xlsx = app.config["REPORTS_XLSX_PATH"]
    if not os.path.exists(xlsx):
        return None

    areas = [a for a in (areas or []) if a in AREAS_VALIDAS]
    if not areas:
        areas = ["Total"]
    if "Total" in areas and len(areas) > 1:
        areas = ["Total"]

    dane = pd.read_excel(xlsx, sheet_name="DANE")
    cols_up = [str(c).upper() for c in dane.columns]
    c_year = dane.columns[2]
    c_area = dane.columns[3]
    c_h = dane.columns[cols_up.index("HOMBRES")]
    c_m = dane.columns[cols_up.index("MUJERES")]
    c_tot = dane.columns[cols_up.index("TOTAL (AMBOS SEXOS)")]

    df = dane[dane[c_area].isin(areas)].copy()

    years = sorted(df[c_year].dropna().astype(int).unique().tolist())
    hombres = []
    mujeres = []
    for y in years:
        sub = df[df[c_year] == y]
        hombres.append(_safe_int(sub[c_h].sum()))
        mujeres.append(_safe_int(sub[c_m].sum()))
    serie_total = [h + m for h, m in zip(hombres, mujeres)]

    last_year = years[-1] if years else None
    res_h = _safe_int(df[df[c_year] == last_year][c_h].sum()) if last_year else 0
    res_m = _safe_int(df[df[c_year] == last_year][c_m].sum()) if last_year else 0

    try:
        reg = pd.read_excel(xlsx, sheet_name="Registro_especial_cund")
        centros = (
            int(reg["ClasePrestadorDesc"].astype(str).str.contains("IPS", na=False).sum())
            or int(len(reg))
        )
    except Exception:
        centros = 0

    proj = []
    if len(years) >= 2:
        dy = years[-1] - years[-2] or 1
        growth = (serie_total[-1] - serie_total[-2]) / dy
        y, val = years[-1], serie_total[-1]
        while y < 2050:
            y += 1
            val += growth
            proj.append({"year": y, "total": int(val)})

    meses = [
        "ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
        "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE",
    ]
    total_2025 = int((res_h + res_m) * 0.02)
    ratio_h = res_h / (res_h + res_m) if (res_h + res_m) else 0.5
    mensuales = [int(total_2025 / 12)] * 12
    vac_h = [int(x * ratio_h) for x in mensuales]
    vac_m = [int(x - vh) for x, vh in zip(mensuales, vac_h)]

    precip = _precipitacion_promedio(municipio_precip)

    pie_labels = ["Hombres", "Mujeres"]
    pie_values = [res_h, res_m]

    return {
        "filtros": {"areas": areas, "anio": last_year},
        "resumen": {
            "hombres": res_h,
            "mujeres": res_m,
            "centros": centros,
            "anio": last_year,
        },
        "crecimiento": {
            "years": years,
            "total": serie_total,
            "hombres": hombres,
            "mujeres": mujeres,
            "forecast": proj,
        },
        "distribucion": {"labels": pie_labels, "values": pie_values},
        "vacunas2025": {"meses": meses, "hombres": vac_h, "mujeres": vac_m},
        "precipitacion": precip,
    }

@app.get("/api/powerdash")
@admin_required
def api_powerdash():
    areas = request.args.get("areas", "")
    areas = [a.strip() for a in areas.split(",") if a.strip()]
    municipio = request.args.get("municipio")
    data = load_excel_summary(areas, municipio)
    if not data:
        return jsonify({"error": "Falta el Excel en data/Proyeccciones_salud_limpia.xlsx"}), 404
    return jsonify(data)

# ------------------------ Mensajes chatbots ------------------------

@app.post("/api/chatbot/message")
def api_chatbot_message():
    """
    Recibe un mensaje del usuario, guarda la conversación
    y devuelve la respuesta del bot.
    """
    db = get_db()
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()

    if not text:
        return jsonify({"error": "Mensaje vacío"}), 400

    # Buscar o crear sesión de chat ligada a la sesión web
    sess_id = session.get("chat_session_id")
    if not sess_id:
        cur = db.execute(
            "INSERT INTO chat_sessions(user_id) VALUES(?)",
            (session.get("user_id"),)
        )
        sess_id = cur.lastrowid
        session["chat_session_id"] = sess_id
        db.commit()

    # Guardar mensaje del usuario
    db.execute(
        "INSERT INTO chat_messages(session_id, sender, message) VALUES(?,?,?)",
        (sess_id, "user", text),
    )

    # Generar respuesta del bot
    reply = generate_chatbot_reply(text)

    # Guardar respuesta del bot
    db.execute(
        "INSERT INTO chat_messages(session_id, sender, message) VALUES(?,?,?)",
        (sess_id, "bot", reply),
    )
    db.commit()

    return jsonify({"reply": reply})


# ------------------------ Reportes (vista) ------------------------

@app.route("/reportes")
@admin_required
def reportes():
    return render_template("reportes.html", user=current_user())

# ------------------------ Contacto ------------------------

@app.post("/contacto")
def contacto():
    flash("Gracias por contactarnos. Te responderemos pronto.", "success")
    return redirect(url_for("index"))

# ------------------------ Generar respuesta chatbots ------------------------
def generate_chatbot_reply(text: str) -> str:
    """
    Respuesta simple del chatbot basada en palabras clave.
    Aquí podrías luego conectar un modelo real (OpenAI, etc.).
    """
    text_low = (text or "").lower()

    base_footer = (
        "\n\n⚠️ Esta orientación es informativa y NO reemplaza una consulta médica. "
        "Si presentas dolor intenso, dificultad para respirar, sangrado abundante "
        "u otra urgencia, acude al servicio de urgencias de tu municipio o llama a la línea de emergencias."
    )

    if "cita" in text_low or "agendar" in text_low or "turno" in text_low:
        reply = (
            "Sobre citas en SaludGo:\n\n"
            "1️⃣ Si ya tienes cuenta, ingresa con tu usuario y ve al menú «Mis citas».\n"
            "2️⃣ Elige el tipo de cita, la fecha y la hora disponible.\n"
            "3️⃣ Si registraste tu correo, te enviaremos confirmación por email.\n\n"
            "Si todavía no tienes cuenta, crea una desde el botón «Ingresar / Registrarme»."
        )
    elif "campaña" in text_low or "brigada" in text_low or "jornada" in text_low:
        reply = (
            "Sobre campañas y brigadas de salud:\n\n"
            "• En la página de inicio puedes ver campañas en las secciones de «Publicaciones» y «Participa».\n"
            "• Si quieres que se programe una jornada en tu zona, puedes contarnos la situación "
            "desde la sección «Participa» en el formulario de reporte de síntomas.\n"
            "• También puedes registrar tu disponibilidad para apoyar como voluntario."
        )
    elif "medicamento" in text_low or "medicina" in text_low or "tratamiento" in text_low:
        reply = (
            "En SaludGo registramos medicamentos que el equipo de salud te ha indicado.\n\n"
            "Puedes verlos en «Mi perfil» → sección «Medicamentos».\n"
            "Si falta un medicamento, la dosis cambió o tienes efectos adversos, "
            "debes comentarlo directamente con tu IPS o en el punto de atención más cercano."
        )
    elif "voluntari" in text_low or "apoyar" in text_low or "ayudar" in text_low:
        reply = (
            "¡Gracias por querer apoyar! 💚\n\n"
            "Para vincularte como voluntario o líder comunitario:\n"
            "1️⃣ Ve a la sección «Participa».\n"
            "2️⃣ Diligencia el formulario de «Disponibilidad para apoyar campañas».\n"
            "3️⃣ El equipo de coordinación usará esa base de datos para planear jornadas y contactarte."
        )
    elif "contacto" in text_low or "teléfono" in text_low or "correo" in text_low:
        reply = (
            "Si necesitas ponerte en contacto con el equipo coordinador:\n\n"
            "• Usa el formulario de «Contáctenos» en la parte final de la página principal.\n"
            "• Allí puedes dejar tus datos y tu mensaje; te responderemos por correo.\n"
            "• Si es una urgencia médica, por favor no uses el formulario, sino los canales de emergencia."
        )
    else:
        reply = (
            "Gracias por tu mensaje. 👋\n\n"
            "Puedo orientarte mejor sobre:\n"
            "• Cómo agendar o modificar citas.\n"
            "• Campañas, brigadas y jornadas de salud.\n"
            "• Medicamentos registrados en la plataforma.\n"
            "• Cómo apoyar como voluntario o líder comunitario.\n\n"
            "Si tu pregunta es más específica, intenta contarme con un poco más de detalle "
            "o escríbenos por el formulario de «Contáctenos»."
        )

    return reply + base_footer


# ------------------------ Main ------------------------

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host='0.0.0.0',debug=True)

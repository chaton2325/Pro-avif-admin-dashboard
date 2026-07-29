import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta

from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///admin.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

DEFAULT_ADMIN_USERNAME = os.environ.get('DEFAULT_ADMIN_USERNAME', 'admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'admin123')
MONGO_TIMEOUT_MS = 3500

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


# --- Modèles SQLite : tout ce qui appartient à CE tableau de bord (jamais aux clients) ---

class AdminUser(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ClientDatabase(db.Model):
    """Une base MongoDB d'un client (chaque client Pro-Avif a son propre backend + sa propre base)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)  # ex: "Ferme Dupont SARL"
    mongo_uri = db.Column(db.String(500), nullable=False)
    db_name = db.Column(db.String(150), nullable=False)  # ex: "pro-avif-db"
    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BlockReason(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_username = db.Column(db.String(150), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(AdminUser, int(user_id))


def log_admin_action(action, details=""):
    entry = ActionLog(
        admin_username=current_user.username if current_user.is_authenticated else "system",
        action=action,
        details=details,
    )
    db.session.add(entry)
    db.session.commit()


def mask_uri(uri):
    """Masque le mot de passe d'une URI Mongo pour l'affichage (jamais la vraie valeur utilisée pour se connecter)."""
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:••••••@", uri or "")


app.jinja_env.filters['mask_uri'] = mask_uri


# --- Connexion directe à la base MongoDB d'un client ---

@contextmanager
def mongo_session(entry: ClientDatabase):
    """Ouvre une connexion à la base MongoDB du client `entry` et la ferme automatiquement.

    Chaque client Pro-Avif a son propre backend + sa propre base : ce tableau de bord ne
    passe par aucune API, il lit/écrit directement dans la base fournie par l'utilisateur.
    """
    client = MongoClient(entry.mongo_uri, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    try:
        client.admin.command('ping')
        yield client[entry.db_name]
    finally:
        client.close()


def test_connection(entry: ClientDatabase):
    try:
        with mongo_session(entry):
            pass
        return True, None
    except Exception as e:
        return False, str(e)


def compute_effective_license(doc):
    """Reproduit la logique du backend (database.get_license_status) côté admin,
    pour tenir compte de l'expiration automatique d'une licence temporaire."""
    doc = doc or {}
    is_blocked = doc.get("is_blocked", False)
    reason = doc.get("block_reason")
    license_end = doc.get("license_end")

    if not is_blocked and doc.get("license_type") == "temporary" and license_end and datetime.utcnow() >= license_end:
        is_blocked = True
        reason = reason or "Licence expirée"

    return {
        "is_blocked": is_blocked,
        "block_reason": reason,
        "license_type": doc.get("license_type", "permanent"),
        "license_start": doc.get("license_start"),
        "license_end": license_end,
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
    }


# --- Authentification ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = AdminUser.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Identifiants invalides.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# --- Tableau de bord : liste des bases clientes + ajout ---

@app.route('/', methods=['GET', 'POST'])
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        mongo_uri = request.form.get('mongo_uri', '').strip()
        db_name = request.form.get('db_name', '').strip()
        notes = request.form.get('notes', '').strip()

        if not name or not mongo_uri or not db_name:
            flash('Nom, URI de connexion et nom de base sont requis.', 'warning')
        elif ClientDatabase.query.filter_by(name=name).first():
            flash('Une base avec ce nom existe déjà.', 'warning')
        else:
            entry = ClientDatabase(name=name, mongo_uri=mongo_uri, db_name=db_name, notes=notes)
            db.session.add(entry)
            db.session.commit()

            ok, err = test_connection(entry)
            if ok:
                flash(f'Base "{name}" ajoutée, connexion vérifiée avec succès.', 'success')
            else:
                flash(f'Base "{name}" ajoutée, mais connexion impossible pour le moment : {err}', 'warning')
            log_admin_action('ADD_DATABASE', name)
        return redirect(url_for('dashboard'))

    databases = ClientDatabase.query.order_by(ClientDatabase.name).all()
    overview = []
    for entry in databases:
        item = {"entry": entry, "reachable": False, "license": None, "user_count": 0, "active_count": 0}
        try:
            with mongo_session(entry) as database:
                users = list(database['users'].find({}, {"isActive": 1}))
                license_doc = database['app_license'].find_one({})
                item["reachable"] = True
                item["user_count"] = len(users)
                item["active_count"] = sum(1 for u in users if u.get("isActive", True))
                item["license"] = compute_effective_license(license_doc)
        except Exception:
            pass
        overview.append(item)

    return render_template('dashboard.html', overview=overview)


@app.route('/databases/<int:db_id>/delete', methods=['POST'])
@login_required
def databases_delete(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    name = entry.name
    db.session.delete(entry)
    db.session.commit()
    log_admin_action('DELETE_DATABASE', name)
    flash(f'Base "{name}" retirée du tableau de bord (les données du client ne sont pas affectées).', 'info')
    return redirect(url_for('dashboard'))


# --- Détail d'une base client : licence & blocage ---

@app.route('/databases/<int:db_id>')
@login_required
def database_detail(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    reasons = BlockReason.query.order_by(BlockReason.label).all()
    status = None
    reachable = False
    try:
        with mongo_session(entry) as database:
            status = compute_effective_license(database['app_license'].find_one({}))
            reachable = True
    except Exception as e:
        flash(f"Connexion à la base impossible : {e}", 'danger')

    return render_template('database_detail.html', db_entry=entry, status=status, reasons=reasons, reachable=reachable)


@app.route('/databases/<int:db_id>/license/block', methods=['POST'])
@login_required
def license_block(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    reason_choice = request.form.get('reason_choice', '').strip()
    custom_reason = request.form.get('custom_reason', '').strip()
    reason = custom_reason if reason_choice == '__custom__' else reason_choice
    if not reason:
        flash('Merci de choisir ou saisir une raison de blocage.', 'warning')
        return redirect(url_for('database_detail', db_id=db_id))

    try:
        with mongo_session(entry) as database:
            existing = database['app_license'].find_one({}) or {}
            database['app_license'].update_one({}, {"$set": {
                "is_blocked": True,
                "block_reason": reason,
                "license_type": existing.get("license_type", "permanent"),
                "license_start": existing.get("license_start", datetime.utcnow()),
                "license_end": existing.get("license_end"),
                "updated_at": datetime.utcnow(),
                "updated_by": current_user.username,
            }}, upsert=True)
        flash(f'Application "{entry.name}" bloquée pour tous les utilisateurs.', 'warning')
        log_admin_action('BLOCK', f"{entry.name}: {reason}")
    except Exception as e:
        flash(f"Échec du blocage : {e}", 'danger')
    return redirect(url_for('database_detail', db_id=db_id))


@app.route('/databases/<int:db_id>/license/unblock', methods=['POST'])
@login_required
def license_unblock(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    try:
        with mongo_session(entry) as database:
            existing = database['app_license'].find_one({}) or {}
            database['app_license'].update_one({}, {"$set": {
                "is_blocked": False,
                "block_reason": None,
                "license_type": existing.get("license_type", "permanent"),
                "license_start": existing.get("license_start", datetime.utcnow()),
                "license_end": existing.get("license_end"),
                "updated_at": datetime.utcnow(),
                "updated_by": current_user.username,
            }}, upsert=True)
        flash(f'Accès rétabli pour tous les utilisateurs de "{entry.name}".', 'success')
        log_admin_action('UNBLOCK', entry.name)
    except Exception as e:
        flash(f"Échec du déblocage : {e}", 'danger')
    return redirect(url_for('database_detail', db_id=db_id))


@app.route('/databases/<int:db_id>/license/set', methods=['POST'])
@login_required
def license_set(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    license_type = request.form.get('license_type', 'permanent')
    license_end = None
    details = 'permanente'

    if license_type == 'temporary':
        duration_days = request.form.get('duration_days', type=int)
        if not duration_days or duration_days <= 0:
            flash('Merci de préciser une durée valide en jours.', 'warning')
            return redirect(url_for('database_detail', db_id=db_id))
        license_end = datetime.utcnow() + timedelta(days=duration_days)
        details = f"temporaire, {duration_days} jour(s)"

    try:
        with mongo_session(entry) as database:
            database['app_license'].update_one({}, {"$set": {
                "is_blocked": False,
                "block_reason": None,
                "license_type": license_type,
                "license_start": datetime.utcnow(),
                "license_end": license_end,
                "updated_at": datetime.utcnow(),
                "updated_by": current_user.username,
            }}, upsert=True)
        flash(f'Licence {details} activée pour "{entry.name}".', 'success')
        log_admin_action('SET_LICENSE', f"{entry.name}: {details}")
    except Exception as e:
        flash(f"Échec de la mise à jour : {e}", 'danger')
    return redirect(url_for('database_detail', db_id=db_id))


# --- Utilisateurs d'une base client ---

@app.route('/databases/<int:db_id>/users')
@login_required
def users_page(db_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    users = []
    reachable = False
    try:
        with mongo_session(entry) as database:
            farms_by_id = {str(f["_id"]): f.get("name") for f in database['fermes'].find({})}
            for u in database['users'].find({}):
                u['_id'] = str(u['_id'])
                u['farmName'] = farms_by_id.get(u.get('farmId')) if u.get('farmId') else None
                users.append(u)
        reachable = True
    except Exception as e:
        flash(f"Connexion à la base impossible : {e}", 'danger')

    return render_template('users.html', db_entry=entry, users=users, reachable=reachable)


@app.route('/databases/<int:db_id>/users/toggle/<user_id>', methods=['POST'])
@login_required
def users_toggle(db_id, user_id):
    entry = ClientDatabase.query.get_or_404(db_id)
    try:
        with mongo_session(entry) as database:
            user = database['users'].find_one({"_id": ObjectId(user_id)})
            if not user:
                flash('Utilisateur introuvable.', 'danger')
            else:
                new_status = not user.get('isActive', True)
                database['users'].update_one({"_id": ObjectId(user_id)}, {"$set": {"isActive": new_status}})
                flash(f"Statut de {user.get('name')} mis à jour.", 'success')
                log_admin_action('TOGGLE_USER', f"{entry.name} / {user.get('name')} -> isActive={new_status}")
    except Exception as e:
        flash(f"Échec de la mise à jour : {e}", 'danger')
    return redirect(url_for('users_page', db_id=db_id))


# --- Raisons de blocage (presets réutilisables pour toutes les bases) ---

@app.route('/reasons', methods=['GET', 'POST'])
@login_required
def reasons_page():
    if request.method == 'POST':
        label = request.form.get('label', '').strip()
        if not label:
            flash('La raison ne peut pas être vide.', 'warning')
        elif BlockReason.query.filter_by(label=label).first():
            flash('Cette raison existe déjà.', 'warning')
        else:
            db.session.add(BlockReason(label=label))
            db.session.commit()
            flash('Raison ajoutée.', 'success')
        return redirect(url_for('reasons_page'))
    reasons = BlockReason.query.order_by(BlockReason.label).all()
    return render_template('reasons.html', reasons=reasons)


@app.route('/reasons/delete/<int:reason_id>', methods=['POST'])
@login_required
def reasons_delete(reason_id):
    reason = BlockReason.query.get_or_404(reason_id)
    db.session.delete(reason)
    db.session.commit()
    flash('Raison supprimée.', 'info')
    return redirect(url_for('reasons_page'))


# --- Équipe admin (comptes de ce tableau de bord) ---

@app.route('/admins', methods=['GET', 'POST'])
@login_required
def admins_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash("Nom d'utilisateur et mot de passe requis.", 'warning')
        elif AdminUser.query.filter_by(username=username).first():
            flash('Cet administrateur existe déjà.', 'warning')
        else:
            new_admin = AdminUser(username=username)
            new_admin.set_password(password)
            db.session.add(new_admin)
            db.session.commit()
            flash('Administrateur ajouté avec succès.', 'success')
        return redirect(url_for('admins_page'))
    admins = AdminUser.query.order_by(AdminUser.username).all()
    return render_template('admins.html', admins=admins)


@app.route('/admins/delete/<int:admin_id>', methods=['POST'])
@login_required
def admins_delete(admin_id):
    if admin_id == current_user.id:
        flash('Vous ne pouvez pas supprimer votre propre compte.', 'warning')
        return redirect(url_for('admins_page'))
    admin = AdminUser.query.get_or_404(admin_id)
    db.session.delete(admin)
    db.session.commit()
    flash('Administrateur supprimé.', 'info')
    return redirect(url_for('admins_page'))


# --- Journal des actions effectuées depuis ce tableau de bord ---

@app.route('/logs')
@login_required
def logs_page():
    logs = ActionLog.query.order_by(ActionLog.timestamp.desc()).limit(200).all()
    return render_template('logs.html', logs=logs)


# --- Initialisation ---

def create_initial_data():
    with app.app_context():
        db.create_all()
        if not AdminUser.query.filter_by(username=DEFAULT_ADMIN_USERNAME).first():
            admin = AdminUser(username=DEFAULT_ADMIN_USERNAME)
            admin.set_password(DEFAULT_ADMIN_PASSWORD)
            db.session.add(admin)
            print(f"Admin par défaut créé : {DEFAULT_ADMIN_USERNAME} / {DEFAULT_ADMIN_PASSWORD}")

        if BlockReason.query.count() == 0:
            for label in [
                "Version d'essai terminée",
                "Non-paiement / facture impayée",
                "Utilisation abusive détectée",
                "Maintenance en cours",
            ]:
                db.session.add(BlockReason(label=label))

        db.session.commit()


if __name__ == '__main__':
    create_initial_data()
    app.run(debug=True, port=5050)

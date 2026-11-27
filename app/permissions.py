"""Flask-AppBuilder login, account administration and WorkBuddy authorization."""
from __future__ import annotations

import secrets
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for
from flask_appbuilder import AppBuilder, IndexView, expose
from flask_appbuilder.const import AUTH_DB
from flask_appbuilder.models.sqla.base import SQLA
from flask_appbuilder.security.sqla.manager import SecurityManager
from flask_appbuilder.security.views import UserDBModelView as FABUserDBModelView
from flask_login import current_user
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from console_routes import dispatch_console_api
from controller import Controller, private_read, private_write

STATIC = Path(__file__).resolve().parent / "static"
VIEW = "WorkBuddy"
READ_ONLY = "WorkBuddy Read Only"
# 开箱即用的默认初始密码：仅限本机绑定（127.0.0.1）使用，局域网绑定会被启动检查拒绝。
DEFAULT_INITIAL_PASSWORD = "123456"
# 默认密码未修改时注入控制台顶部的提示条（可关闭，按浏览器会话记忆）。
INITIAL_PASSWORD_BANNER = (
    '<div id="wb-initial-banner" role="status" style="background:#b45309;color:#fff;'
    'padding:8px 16px;font:13px/1.6 system-ui,sans-serif;display:flex;gap:12px;align-items:center;">'
    '<span>当前使用默认密码 123456，<a href="/initial-password" style="color:#fff;'
    'text-decoration:underline;">请尽快修改</a>；未修改前无法绑定局域网地址。</span>'
    '<button type="button" aria-label="关闭提示" onclick="sessionStorage.setItem(\'wbInitialBanner\',\'1\');'
    'this.parentElement.remove()" style="margin-left:auto;background:none;border:none;color:#fff;'
    'cursor:pointer;font-size:16px;padding:0 4px;">×</button></div>'
    '<script>try{if(sessionStorage.getItem("wbInitialBanner"))'
    '{document.getElementById("wb-initial-banner").remove();}}catch(e){}</script>'
)
PERMISSIONS = {
    ("GET", "summary"): "status",
    ("GET", "session"): "accounts",
    ("GET", "hermes/key"): "keys",
    ("GET", "providers"): "providers",
    ("POST", "providers/add"): "providers",
    ("POST", "providers/edit"): "providers",
    ("POST", "providers/remove"): "providers",
    ("POST", "accounts"): "accounts",
    ("POST", "accounts/move"): "accounts",
    ("POST", "accounts/promote"): "accounts",
    ("POST", "accounts/remove"): "accounts",
    ("POST", "session/cancel"): "accounts",
    ("POST", "labels"): "accounts",
    ("POST", "reserve"): "accounts",
    ("POST", "pins"): "accounts",
    ("POST", "checkin/manual"): "checkin",
    ("POST", "checkin/all"): "checkin",
    ("POST", "checkin"): "checkin",
    ("POST", "checkin/settings"): "checkin_settings",
}


class UserDBModelView(FABUserDBModelView):
    formatters_columns = {
        **(FABUserDBModelView.formatters_columns or {}),
        "active": lambda value: "是" if value else "否",
    }


class ConsoleSecurityManager(SecurityManager):
    userdbmodelview = UserDBModelView


class SecurityIndex(IndexView):
    route_base = "/security"

    @expose("/")
    def index(self):
        if not current_user.is_authenticated or not current_user.is_active:
            return redirect(url_for("AuthDBView.login"))
        return super().index()


def _signing_key(path: Path) -> str:
    try:
        return private_read(path).strip()
    except FileNotFoundError:
        key = secrets.token_urlsafe(48)
        private_write(path, key + "\n")
        return key


def _initialize(appbuilder: AppBuilder, db: SQLA, password: str | None, bind_ip: str) -> None:
    sm = appbuilder.sm
    session = db.session
    session.execute(text("CREATE TABLE IF NOT EXISTS wb_initial_admin (user_id INTEGER PRIMARY KEY)"))
    session.commit()
    admin_role = sm.find_role("Admin")
    for action in sorted(set(PERMISSIONS.values())):
        permission = sm.add_permission_view_menu("can_" + action, VIEW)
        sm.add_permission_role(admin_role, permission)
    if sm.find_role(READ_ONLY) is None:
        role = sm.add_role(READ_ONLY)
        sm.add_permission_role(role, sm.find_permission_view_menu("can_status", VIEW))

    pending_id = session.execute(text("SELECT user_id FROM wb_initial_admin LIMIT 1")).scalar()
    if pending_id is not None:
        pending_user = sm.get_user_by_id(pending_id) if pending_id else None
        # 局域网绑定拒绝仍在使用默认密码的安装，倒逼先在本机改密。
        if bind_ip != "127.0.0.1" and pending_user and check_password_hash(pending_user.password, DEFAULT_INITIAL_PASSWORD):
            raise RuntimeError("请先在本机修改初始密码，再启用局域网访问")
        return
    # A persistent installation marker also covers installations whose first
    # administrator was renamed or deleted after initialization.
    admin = sm.find_user(username="admin")
    if admin is None:
        # 未提供 bootstrap 密码时回退到默认密码，让本机部署开箱可用；
        # 局域网部署仍要求在 data/web/permissions-bootstrap 写入至少 12 个字符。
        password = (password or "").strip() or DEFAULT_INITIAL_PASSWORD
        if bind_ip != "127.0.0.1" and len(password) < 12:
            raise RuntimeError(
                "请在私有数据目录 data/web/permissions-bootstrap 配置至少 12 个字符的初始密码"
                "（本机部署默认使用 admin/123456，无需配置）"
            )
        admin = sm.user_model(username="admin", first_name="WorkBuddy", last_name="Admin",
                              email="admin@example.invalid", active=True, roles=[admin_role],
                              password=generate_password_hash(password))
        session.add(admin)
        session.flush()
    # 仅当实际生效的密码是默认值时才挂出"待改密"标记；自定义 bootstrap 密码不需要提醒。
    if password == DEFAULT_INITIAL_PASSWORD:
        session.execute(text("INSERT INTO wb_initial_admin (user_id) VALUES (:id)"), {"id": admin.id})
    session.commit()


def build_permissions_app(controller: Controller, web_data: Path, *, bootstrap_password: str | None, bind_ip: str) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=_signing_key(web_data / "permissions.secret"),
        SQLALCHEMY_DATABASE_URI="sqlite:///" + str(web_data / "permissions.sqlite3"),
        AUTH_TYPE=AUTH_DB,
        APP_NAME="WorkBuddy 权限管理",
        BABEL_DEFAULT_LOCALE="zh",
        BABEL_TRANSLATION_DIRECTORIES="translations",
        LANGUAGES={"zh": {"flag": "cn", "name": "简体中文"}},
        AUTH_USER_REGISTRATION=False,
        AUTH_RATE_LIMITED=True,
        AUTH_RATE_LIMIT="10 per minute",
        SESSION_COOKIE_NAME="wb_permissions",
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        LOGOUT_REDIRECT_URL="/login/",
        MAX_CONTENT_LENGTH=1_000_000,
    )
    @app.before_request
    def use_chinese_locale():
        if session.get("locale") != "zh":
            session["locale"] = "zh"

    db = SQLA(app)
    CSRFProtect(app)
    with app.app_context():
        builder = AppBuilder(app, db.session, indexview=SecurityIndex, security_manager_class=ConsoleSecurityManager)
        _initialize(builder, db, bootstrap_password, bind_ip)
        builder.add_link("返回控制台", href="/")

    def allowed(action: str) -> bool:
        return (
            current_user.is_authenticated and current_user.is_active
            and builder.sm.has_access("can_" + action, VIEW)
        )

    def initial_password_pending() -> bool:
        """默认密码尚未修改的标记：控制台提示条与 /api/summary 能力位都依赖它。"""
        if not current_user.is_authenticated or not current_user.is_active:
            return False
        return bool(db.session.execute(text(
            "SELECT 1 FROM wb_initial_admin WHERE user_id = :id"
        ), {"id": current_user.id}).scalar())

    @app.route("/initial-password", methods=["GET", "POST"])
    def initial_password():
        if not current_user.is_authenticated or not current_user.is_active:
            return redirect(url_for("AuthDBView.login"))
        message = ""
        if request.method == "POST":
            old = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            if len(old) > 256 or not check_password_hash(current_user.password, old):
                message = "当前密码错误"
            elif not 6 <= len(new) <= 256 or new in (old, DEFAULT_INITIAL_PASSWORD):
                message = "请设置 6 至 256 字符的新密码，且不能与当前或默认密码相同"
            else:
                current_user.password = generate_password_hash(new)
                db.session.execute(text("UPDATE wb_initial_admin SET user_id = 0 WHERE user_id = :id"),
                                   {"id": current_user.id})
                db.session.commit()
                return redirect("/")
        return render_template_string("""
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>修改控制台密码</title>
<body><main><h1>修改控制台密码</h1>
{% if message %}<p role="alert">{{ message }}</p>{% endif %}
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<p><label>当前密码 <input name="current_password" type="password" autocomplete="current-password" required></label></p>
<p><label>新密码 <input name="new_password" type="password" autocomplete="new-password" minlength="6" maxlength="256" required></label></p>
<button type="submit">保存密码</button>
</form><p><a href="{{ url_for('AuthDBView.logout') }}">退出</a> · <a href="/">返回控制台</a></p>
</main></body></html>
""", message=message)

    @app.route("/login")
    def login_alias():
        return redirect(url_for("AuthDBView.login"))

    @app.get("/healthz")
    def health():
        return "", 200 if controller.gateway.alive() else 503

    @app.get("/")
    @app.get("/index.html")
    def console():
        if not current_user.is_authenticated or not current_user.is_active:
            return redirect(url_for("AuthDBView.login", next="/"))
        if not allowed("status"):
            abort(403)
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        page = page.replace('name="wb-csrf" content=""', f'name="wb-csrf" content="{generate_csrf()}"', 1)
        if initial_password_pending():
            page = page.replace("<body>", "<body>" + INITIAL_PASSWORD_BANNER, 1)
        return page

    @app.get("/providers.js")
    def provider_script():
        return (
            (STATIC / "providers.js").read_text(encoding="utf-8"), 200,
            {"Content-Type": "text/javascript; charset=utf-8"},
        )

    @app.route("/api/<path:resource>", methods=["GET", "POST"])
    def api(resource):
        if not current_user.is_authenticated or not current_user.is_active:
            return jsonify(error="请先登录"), 401
        action = PERMISSIONS.get((request.method, resource))
        if action is None:
            return jsonify(error="接口不存在"), 404
        if not allowed(action):
            return jsonify(error="当前角色没有此操作权限"), 403
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="跨站请求被拒绝"), 403
        body = request.get_json(silent=True) if request.method == "POST" else {}
        if not isinstance(body, dict):
            return jsonify(error="请求体必须是 JSON 对象"), 400
        try:
            code, result = dispatch_console_api(controller, request.method, "/api/" + resource,
                                                request.args.to_dict(flat=False), body)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if resource == "summary":
            result = dict(result)
            capabilities = {name: allowed(name) for name in set(PERMISSIONS.values())}
            capabilities["admin"] = builder.sm.has_access("can_list", "UserDBModelView")
            capabilities["initialPassword"] = initial_password_pending()
            result["permissions"] = capabilities
            if not capabilities["accounts"]:
                result["sessions"] = []
            if not capabilities["keys"]:
                result["hermes"] = dict(result["hermes"], keyMasked="", hasKey=False)
        return jsonify(result), code

    @app.after_request
    def prevent_caching(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    return app

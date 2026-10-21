import os
import re
import math
import json
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import click
import cloudinary
import cloudinary.uploader
import cloudinary.utils
from cloudinary.exceptions import Error as CloudinaryError
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
database_url = os.environ.get("DATABASE_URL", "sqlite:///shop.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
if database_url.startswith("postgresql://"):
    database_url = database_url.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
if os.environ.get("CLOUDINARY_URL"):
    cloudinary.config(secure=True)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["WHATSAPP_ACCESS_TOKEN"] = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
app.config["WHATSAPP_PHONE_NUMBER_ID"] = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
app.config["WHATSAPP_TEMPLATE_NAME"] = os.environ.get("WHATSAPP_TEMPLATE_NAME", "")
app.config["WHATSAPP_TEMPLATE_LANGUAGE"] = os.environ.get(
    "WHATSAPP_TEMPLATE_LANGUAGE", "en"
)
app.config["WHATSAPP_API_VERSION"] = os.environ.get(
    "WHATSAPP_API_VERSION", "v23.0"
)
MAX_PRODUCT_IMAGES = 10
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
IMAGE_SIGNATURES = {
    ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    ".gif": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
    ".webp": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
}

db = SQLAlchemy(app)
csrf = CSRFProtect(app)

BOOKING_STATUSES = [
    "New", "Accepted", "Preparing", "Ready for Pickup", "Collected", "Rejected", "Cancelled",
]
DELIVERY_FEE_PER_KM = 5
LOW_STOCK_THRESHOLD = 5
CATEGORIES = ["Men's Clothing", "Women's Clothing", "Shoes", "Chappals & Sandals", "Accessories", "Other"]
SIZE_OPTIONS = [
    "XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL",
    "Free Size", *(str(size) for size in range(1, 16)),
]
COLOR_OPTIONS = [
    "Black", "White", "Red", "Blue", "Navy", "Green", "Yellow", "Pink",
    "Purple", "Orange", "Brown", "Grey", "Beige", "Cream", "Maroon",
    "Teal", "Olive", "Gold", "Silver", "Multicolor",
]


class ShopPathMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        match = re.match(r"^/shop/([a-z0-9-]+)(/.*)?$", path)
        if match:
            prefix = f"/shop/{match.group(1)}"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + prefix
            environ["PATH_INFO"] = match.group(2) or "/"
            environ["fashion_shop_slug"] = match.group(1)
        return self.application(environ, start_response)


class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), nullable=False, unique=True)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    delivery_enabled = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    owners = db.relationship("Admin", backref="shop", cascade="all, delete-orphan")


def safe_next_url(target):
    if not target or "\\" in target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return target


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, index=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Numeric(10, 2), nullable=False)
    image_url = db.Column(db.String(1000), default="")
    sizes = db.Column(db.String(300), default="")
    colors = db.Column(db.String(300), default="")
    stock = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    images = db.relationship("ProductImage", backref="product", cascade="all, delete-orphan", order_by="ProductImage.id")


class ProductImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    image_path = db.Column(db.String(1000), nullable=False)


class PaymentSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, unique=True)
    qr_image_path = db.Column(db.String(1000), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class StorefrontSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, unique=True)
    shop_image_path = db.Column(db.String(1000), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    images = db.relationship(
        "StorefrontImage",
        backref="settings",
        cascade="all, delete-orphan",
        order_by="StorefrontImage.id",
    )


class StorefrontImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    settings_id = db.Column(db.Integer, db.ForeignKey("storefront_settings.id"), nullable=False)
    image_path = db.Column(db.String(1000), nullable=False)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), nullable=False, index=True)
    booking_code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    whatsapp_updates_opt_in = db.Column(
        db.Boolean, nullable=False, default=False
    )
    whatsapp_opted_in_at = db.Column(db.DateTime)
    pickup_at = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.String(500), default="")
    fulfillment = db.Column(db.String(20), nullable=False, default="pickup")
    delivery_latitude = db.Column(db.Float)
    delivery_longitude = db.Column(db.Float)
    delivery_distance_km = db.Column(db.Float)
    delivery_distance_method = db.Column(
        db.String(20), nullable=False, default="straight_line"
    )
    delivery_fee = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(40), nullable=False, default="New")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    items = db.relationship("BookingItem", backref="booking", cascade="all, delete-orphan", lazy=True)
    payment = db.relationship("BookingPayment", backref="booking", cascade="all, delete-orphan", uselist=False)


class BookingPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=False, unique=True)
    method = db.Column(db.String(20), nullable=False)


class BookingItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    product_name = db.Column(db.String(160), nullable=False)
    size = db.Column(db.String(40), default="")
    color = db.Column(db.String(60), default="")
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    product = db.relationship("Product")


app.wsgi_app = ShopPathMiddleware(app.wsgi_app)


@app.before_request
def load_shop_context():
    shop_slug = request.environ.get("fashion_shop_slug")
    g.shop = Shop.query.filter_by(slug=shop_slug).first() if shop_slug else None
    if shop_slug and g.shop is None:
        abort(404)
    if g.shop and session.get("admin_id"):
        owner = db.session.get(Admin, session["admin_id"])
        if not owner or owner.shop_id != g.shop.id:
            session.pop("admin_id", None)
    if not shop_slug and request.endpoint not in {"home", "owner_signup", "owner_login", "health", "static"}:
        return redirect(url_for("home"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        owner = db.session.get(Admin, session.get("admin_id")) if session.get("admin_id") else None
        if not owner or not g.shop or owner.shop_id != g.shop.id:
            session.pop("admin_id", None)
            flash("Please sign in to access the owner dashboard.", "warning")
            return redirect(url_for("admin_login", next=request.script_root + request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    latest_booking_id = 0
    if session.get("admin_id") and getattr(g, "shop", None):
        latest_booking_id = (
            db.session.query(db.func.max(Booking.id))
            .filter_by(shop_id=g.shop.id)
            .scalar()
            or 0
        )
    return {
        "shop_name": g.shop.name if getattr(g, "shop", None) else "Fashion Store Directory",
        "current_shop": getattr(g, "shop", None),
        "latest_booking_id": latest_booking_id,
    }


@app.template_filter("money")
def money(value):
    return f"₹{float(value):,.2f}"


@app.template_filter("datetime_local")
def datetime_local(value):
    return value.strftime("%Y-%m-%dT%H:%M") if value else ""


@app.template_filter("product_image_url")
def product_image_url(image_path):
    if image_path.startswith("cloudinary:"):
        public_id = image_path.removeprefix("cloudinary:")
        return cloudinary.utils.cloudinary_url(public_id, secure=True)[0]
    if image_path.startswith("uploads/"):
        return url_for("static", filename=image_path)
    return image_path


@app.template_filter("product_primary_image")
def product_primary_image(product):
    image_paths = product_image_paths(product)
    return product_image_url(image_paths[0]) if image_paths else ""


def product_image_paths(product):
    paths = []
    if product.image_url:
        paths.append(product.image_url)
    paths.extend(image.image_path for image in product.images)
    return list(dict.fromkeys(paths))


def validate_uploaded_image(uploaded_image):
    extension = os.path.splitext(uploaded_image.filename)[1].lower()
    image_data = uploaded_image.read()
    if extension not in IMAGE_SIGNATURES or not IMAGE_SIGNATURES[extension](image_data):
        raise ValueError("Upload a valid JPG, PNG, GIF or WEBP image.")
    return extension, image_data


def save_image_data(image_data, extension, folder):
    if os.environ.get("CLOUDINARY_URL"):
        try:
            result = cloudinary.uploader.upload(
                BytesIO(image_data),
                folder=folder,
                resource_type="image",
                unique_filename=True,
            )
        except CloudinaryError as error:
            app.logger.exception("Unable to upload image to Cloudinary")
            raise OSError("The image could not be saved to cloud storage.") from error
        public_id = result.get("public_id")
        if not public_id:
            raise OSError("Cloud storage did not return an image identifier.")
        return f"cloudinary:{public_id}"

    filename = f"{uuid4().hex}{extension}"
    relative_path = os.path.join("uploads", folder, filename).replace("\\", "/")
    upload_directory = os.path.join(app.static_folder, "uploads", folder)
    os.makedirs(upload_directory, exist_ok=True)
    with open(os.path.join(upload_directory, filename), "wb") as image_file:
        image_file.write(image_data)
    return relative_path


def save_uploaded_image(uploaded_image, folder):
    extension, image_data = validate_uploaded_image(uploaded_image)
    return save_image_data(image_data, extension, folder)


def get_payment_settings():
    if not g.shop:
        return None
    return PaymentSettings.query.filter_by(shop_id=g.shop.id).first()


def get_storefront_settings():
    if not g.shop:
        return None
    return StorefrontSettings.query.filter_by(shop_id=g.shop.id).first()


def storefront_image_paths(settings):
    if not settings:
        return []
    paths = [settings.shop_image_path]
    paths.extend(image.image_path for image in settings.images)
    return list(dict.fromkeys(paths))


def remove_uploaded_image_file(image_path):
    if not image_path:
        return True
    if (
        Product.query.filter_by(image_url=image_path).first()
        or ProductImage.query.filter_by(image_path=image_path).first()
        or PaymentSettings.query.filter_by(qr_image_path=image_path).first()
        or StorefrontSettings.query.filter_by(shop_image_path=image_path).first()
        or StorefrontImage.query.filter_by(image_path=image_path).first()
    ):
        return True

    if image_path.startswith("cloudinary:"):
        public_id = image_path.removeprefix("cloudinary:")
        try:
            result = cloudinary.uploader.destroy(public_id, resource_type="image")
        except CloudinaryError:
            app.logger.exception("Unable to remove image from Cloudinary: %s", public_id)
            return False
        if result.get("result") == "ok":
            return True
        app.logger.error(
            "Cloudinary did not delete image %s: %s",
            public_id,
            result.get("result", "unknown result"),
        )
        return False

    if not image_path.startswith("uploads/"):
        return True
    upload_root = os.path.abspath(os.path.join(app.static_folder, "uploads"))
    file_path = os.path.abspath(os.path.join(app.static_folder, image_path))
    try:
        is_upload = os.path.commonpath([upload_root, file_path]) == upload_root
    except ValueError:
        is_upload = False
    if not is_upload or file_path == upload_root:
        app.logger.error("Refusing to remove uploaded image outside uploads: %s", image_path)
        return False
    try:
        os.remove(file_path)
    except FileNotFoundError:
        app.logger.warning("Uploaded image file was already missing: %s", image_path)
        return False
    except OSError:
        app.logger.exception("Unable to remove uploaded image file: %s", image_path)
        return False
    return True


def remove_image_files(image_paths):
    removed = True
    for image_path in image_paths:
        if not remove_uploaded_image_file(image_path):
            removed = False
    return removed


def coordinates_are_valid(latitude, longitude):
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


class DeliveryRouteError(Exception):
    pass


def driving_route_distance_km(start_latitude, start_longitude, end_latitude, end_longitude):
    coordinates = (
        f"{start_longitude:.7f},{start_latitude:.7f};"
        f"{end_longitude:.7f},{end_latitude:.7f}"
    )
    query = urlencode({"overview": "false", "alternatives": "false", "steps": "false"})
    request = Request(
        f"https://router.project-osrm.org/route/v1/driving/{coordinates}?{query}",
        headers={"User-Agent": "LocalFashionBooking/1.0"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        app.logger.warning("Unable to calculate driving route: %s", error)
        raise DeliveryRouteError(
            "Could not calculate a road route right now. Please try again."
        ) from error

    if (
        not isinstance(result, dict)
        or result.get("code") != "Ok"
        or not result.get("routes")
    ):
        result_code = (
            result.get("code", "unknown")
            if isinstance(result, dict)
            else "invalid response"
        )
        app.logger.warning("Routing service returned no road route: %s", result_code)
        raise DeliveryRouteError(
            "No driving route was found for this location. Check the map pin or choose pickup."
        )
    try:
        distance_meters = float(result["routes"][0]["distance"])
    except (KeyError, TypeError, ValueError) as error:
        app.logger.error("Routing service returned an invalid route distance")
        raise DeliveryRouteError(
            "Could not calculate a road route right now. Please try again."
        ) from error
    if not math.isfinite(distance_meters) or distance_meters < 0:
        app.logger.error("Routing service returned an invalid route distance")
        raise DeliveryRouteError(
            "Could not calculate a road route right now. Please try again."
        )
    return distance_meters / 1000


def send_whatsapp_status_notification(booking, shop_name):
    required_settings = (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_TEMPLATE_NAME",
    )
    missing_settings = [
        setting for setting in required_settings if not app.config[setting]
    ]
    if missing_settings:
        app.logger.warning(
            "WhatsApp status update was not sent; missing configuration: %s",
            ", ".join(missing_settings),
        )
        return False

    phone_number = "".join(
        character for character in booking.phone if character.isdigit()
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": app.config["WHATSAPP_TEMPLATE_NAME"],
            "language": {"code": app.config["WHATSAPP_TEMPLATE_LANGUAGE"]},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": booking.customer_name},
                        {"type": "text", "text": booking.booking_code},
                        {"type": "text", "text": booking.status},
                        {"type": "text", "text": shop_name},
                    ],
                }
            ],
        },
    }
    api_version = app.config["WHATSAPP_API_VERSION"]
    phone_number_id = app.config["WHATSAPP_PHONE_NUMBER_ID"]
    endpoint = (
        f"https://graph.facebook.com/{api_version}/"
        f"{phone_number_id}/messages"
    )
    message_request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + app.config["WHATSAPP_ACCESS_TOKEN"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(message_request, timeout=10) as response:
            result = json.load(response)
    except HTTPError as error:
        app.logger.warning(
            "WhatsApp status update failed with HTTP status %s",
            error.code,
        )
        return False
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        app.logger.warning("WhatsApp status update failed: %s", error)
        return False

    if (
        not isinstance(result, dict)
        or not isinstance(result.get("messages"), list)
        or not result["messages"]
    ):
        app.logger.warning("WhatsApp API returned no message confirmation")
        return False
    return True


@app.post("/delivery/estimate")
def delivery_estimate():
    if not g.shop or not g.shop.delivery_enabled or g.shop.latitude is None or g.shop.longitude is None:
        return jsonify(error="Delivery is not enabled for this shop."), 400
    try:
        customer_latitude = float(request.form.get("latitude", ""))
        customer_longitude = float(request.form.get("longitude", ""))
    except (TypeError, ValueError):
        return jsonify(error="Share a valid delivery location first."), 400
    if not coordinates_are_valid(customer_latitude, customer_longitude):
        return jsonify(error="Share a valid delivery location first."), 400
    try:
        distance_km = driving_route_distance_km(
            g.shop.latitude,
            g.shop.longitude,
            customer_latitude,
            customer_longitude,
        )
    except DeliveryRouteError as error:
        return jsonify(error=str(error)), 503
    billable_km = math.ceil(distance_km - 1e-9)
    return jsonify(
        distance_km=round(distance_km, 2),
        billable_km=billable_km,
        delivery_fee=billable_km * DELIVERY_FEE_PER_KM,
    )


@app.get("/")
def home():
    if not g.shop:
        shops = Shop.query.order_by(Shop.name.asc()).all()
        return render_template("directory.html", shops=shops)

    category_cards = []
    active_products = Product.query.filter_by(shop_id=g.shop.id, active=True).order_by(Product.created_at.desc()).all()
    for category in CATEGORIES[:-1]:
        category_product = next(
            (product for product in active_products
             if product.category == category and product_image_paths(product)),
            None,
        )
        category_cards.append({
            "name": category,
            "image": product_primary_image(category_product) if category_product else "",
        })
    featured = Product.query.filter_by(shop_id=g.shop.id, active=True).order_by(Product.created_at.desc()).limit(8).all()
    storefront_settings = get_storefront_settings()
    return render_template(
        "home.html",
        categories=category_cards,
        featured=featured,
        shop_images=[
            product_image_url(image_path)
            for image_path in storefront_image_paths(storefront_settings)
        ],
    )


@app.route("/owner-signup", methods=["GET", "POST"])
def owner_signup():
    if request.method == "POST":
        shop_name = request.form.get("shop_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not shop_name or len(shop_name) > 120:
            flash("Enter a shop name between 1 and 120 characters.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400
        if not username or len(username) > 80:
            flash("Enter a username between 1 and 80 characters.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400
        if len(password) < 12:
            flash("Use a password with at least 12 characters.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400
        if password != confirm_password:
            flash("The passwords do not match.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400
        if Admin.query.filter_by(username=username).first():
            flash("That username is already in use. Choose another username.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400

        base_slug = re.sub(r"[^a-z0-9]+", "-", shop_name.lower()).strip("-")[:70] or "shop"
        slug = base_slug
        suffix = 2
        while Shop.query.filter_by(slug=slug).first():
            ending = f"-{suffix}"
            slug = f"{base_slug[:80 - len(ending)]}{ending}"
            suffix += 1

        shop = Shop(name=shop_name, slug=slug)
        owner = Admin(username=username)
        owner.set_password(password)
        shop.owners.append(owner)
        db.session.add(shop)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Could not create this shop account. Please try another shop name or username.", "danger")
            return render_template("owner_signup.html", shop_name=shop_name, username=username), 400

        session.clear()
        session["admin_id"] = owner.id
        session.permanent = True
        flash("Your shop and owner account are ready.", "success")
        return redirect(f"/shop/{slug}/admin")

    return render_template("owner_signup.html", shop_name="", username="")


@app.route("/owner-login", methods=["GET", "POST"])
def owner_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        owner = Admin.query.filter_by(username=username).first()
        if not owner or not owner.check_password(password):
            flash("Invalid username or password.", "danger")
            return render_template("owner_login.html", username=username), 401

        session.clear()
        session["admin_id"] = owner.id
        session.permanent = True
        flash("Signed in successfully.", "success")
        return redirect(f"/shop/{owner.shop.slug}/admin")

    return render_template("owner_login.html", username="")


@app.get("/products")
def products():
    if not g.shop:
        return redirect(url_for("home"))
    category = request.args.get("category", "").strip()
    q = request.args.get("q", "").strip()
    query = Product.query.filter_by(shop_id=g.shop.id, active=True)
    if category:
        query = query.filter_by(category=category)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.name.ilike(like), Product.description.ilike(like), Product.category.ilike(like)))
    items = query.order_by(Product.name.asc()).all()
    return render_template("products.html", products=items, categories=CATEGORIES, selected_category=category, q=q)


@app.get("/product/<int:product_id>")
def product_detail(product_id):
    if not g.shop:
        return redirect(url_for("home"))
    product = Product.query.filter_by(id=product_id, shop_id=g.shop.id, active=True).first_or_404()
    return render_template("product_detail.html", product=product, product_images=product_image_paths(product))


@app.route("/book", methods=["GET", "POST"])
def book():
    if not g.shop:
        return redirect(url_for("home"))
    if request.method == "GET":
        ids = request.args.get("product_ids", "")
        selected_ids = []
        for part in ids.split(","):
            if part.strip().isdigit():
                selected_ids.append(int(part.strip()))
        selected = (
            Product.query.filter(
                Product.id.in_(selected_ids),
                Product.shop_id == g.shop.id,
                Product.active.is_(True),
            ).all()
            if selected_ids else []
        )
        products = (
            selected if ids.strip()
            else Product.query.filter_by(shop_id=g.shop.id, active=True).order_by(Product.name).all()
        )
        if ids.strip() and not selected:
            flash("That product is no longer available. Please choose another item.", "warning")
            return redirect(url_for("products"))
        return render_template(
            "book.html",
            products=products,
            selected=selected,
            payment_settings=get_payment_settings(),
            single_product_booking=len(products) == 1 and bool(selected),
            delivery_enabled=(
                g.shop.delivery_enabled
                and g.shop.latitude is not None
                and g.shop.longitude is not None
            ),
        )

    customer_name = request.form.get("customer_name", "").strip()
    phone = request.form.get("phone", "").strip()
    whatsapp_updates_opt_in = (
        request.form.get("whatsapp_updates_opt_in") == "on"
    )
    pickup_raw = request.form.get("pickup_at", "").strip()
    notes = request.form.get("notes", "").strip()[:500]
    payment_method = request.form.get("payment_method", "shop")
    fulfillment = request.form.get("fulfillment", "pickup")
    payment_settings = get_payment_settings()
    if fulfillment not in ("pickup", "delivery"):
        flash("Choose pickup or delivery.", "danger")
        return redirect(url_for("book"))

    delivery_latitude = None
    delivery_longitude = None
    delivery_distance_km = None
    delivery_fee = 0
    if fulfillment == "delivery":
        if (
            not g.shop.delivery_enabled
            or g.shop.latitude is None
            or g.shop.longitude is None
        ):
            flash("This shop has not enabled delivery or saved its location.", "warning")
            return redirect(url_for("book"))
        try:
            delivery_latitude = float(request.form.get("delivery_latitude", ""))
            delivery_longitude = float(request.form.get("delivery_longitude", ""))
        except (TypeError, ValueError):
            flash("Share your current location to request delivery.", "danger")
            return redirect(url_for("book"))
        if not coordinates_are_valid(delivery_latitude, delivery_longitude):
            flash("The shared delivery location is not valid. Please share it again.", "danger")
            return redirect(url_for("book"))
        try:
            delivery_distance_km = driving_route_distance_km(
                g.shop.latitude,
                g.shop.longitude,
                delivery_latitude,
                delivery_longitude,
            )
        except DeliveryRouteError as error:
            flash(str(error), "danger")
            return redirect(url_for("book"))
        delivery_fee = (
            math.ceil(delivery_distance_km - 1e-9) * DELIVERY_FEE_PER_KM
        )
        if payment_method != "cod":
            flash("Delivery orders are paid in cash on delivery.", "danger")
            return redirect(url_for("book"))
    elif payment_method not in ("shop", "online") or (
        payment_method == "online" and not payment_settings
    ):
        flash("Choose an available payment option.", "danger")
        return redirect(url_for("book"))
    if len(customer_name) < 2 or len(customer_name) > 120:
        flash("Enter a customer name between 2 and 120 characters.", "danger")
        return redirect(url_for("book"))
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        flash("Enter a valid mobile number (10–15 digits).", "danger")
        return redirect(url_for("book"))
    normalized_phone = "".join(
        character for character in phone if character not in " ()-."
    )
    if whatsapp_updates_opt_in and not re.fullmatch(
        r"\+[1-9]\d{7,14}", normalized_phone
    ):
        flash(
            "For WhatsApp updates, enter your number with its country code, "
            "such as +91 98765 43210.",
            "danger",
        )
        return redirect(url_for("book"))
    try:
        pickup_at = datetime.strptime(pickup_raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Select an expected shop pickup date and time.", "danger")
        return redirect(url_for("book"))
    if pickup_at < datetime.now() - timedelta(minutes=1):
        flash("Pickup time cannot be in the past.", "danger")
        return redirect(url_for("book"))

    quantities = {}
    for key, value in request.form.items():
        if key.startswith("qty_"):
            product_id = key[4:]
            if product_id.isdigit():
                try:
                    qty = int(value)
                except (TypeError, ValueError):
                    qty = 0
                if qty > 0:
                    quantities[int(product_id)] = min(qty, 10)
    if not quantities:
        flash("Choose at least one product and quantity.", "danger")
        return redirect(url_for("book"))

    chosen = Product.query.filter(
        Product.id.in_(list(quantities.keys())),
        Product.shop_id == g.shop.id,
        Product.active.is_(True),
    ).all()
    if len(chosen) != len(quantities):
        flash("One or more selected products are no longer available. Please review your selection.", "danger")
        return redirect(url_for("book"))
    for p in chosen:
        if p.stock < quantities[p.id]:
            flash(f"Not enough stock for {p.name}. Available stock: {p.stock}.", "danger")
            return redirect(url_for("book"))

    try:
        booking = Booking(
            shop_id=g.shop.id,
            booking_code=f"BK{datetime.utcnow().strftime('%y%m%d')}{os.urandom(2).hex().upper()}",
            customer_name=customer_name,
            phone=phone,
            whatsapp_updates_opt_in=whatsapp_updates_opt_in,
            whatsapp_opted_in_at=(
                datetime.utcnow() if whatsapp_updates_opt_in else None
            ),
            pickup_at=pickup_at,
            notes=notes,
            fulfillment=fulfillment,
            delivery_latitude=delivery_latitude,
            delivery_longitude=delivery_longitude,
            delivery_distance_km=delivery_distance_km,
            delivery_distance_method="road",
            delivery_fee=delivery_fee,
            status="New",
        )
        booking.payment = BookingPayment(method=payment_method)
        db.session.add(booking)
        db.session.flush()
        for p in chosen:
            qty = quantities[p.id]
            p.stock -= qty
            db.session.add(BookingItem(
                booking_id=booking.id, product_id=p.id, product_name=p.name,
                size=request.form.get(f"size_{p.id}", "").strip()[:40],
                color=request.form.get(f"color_{p.id}", "").strip()[:60],
                quantity=qty, unit_price=p.price
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Booking creation failed")
        flash("We could not save your booking. Please try again.", "danger")
        return redirect(url_for("book"))

    return render_template(
        "booking_success.html",
        booking=booking,
        payment_settings=payment_settings,
        items_subtotal=sum(
            (item.unit_price * item.quantity for item in booking.items),
            start=0,
        ),
    )


@app.get("/booking-status")
def booking_status_lookup():
    if not g.shop:
        return redirect(url_for("home"))
    booking_code = request.args.get("booking_code", "").strip().upper()
    if not booking_code:
        return render_template(
            "booking_lookup.html",
            booking=None,
            booking_code="",
        )
    booking = Booking.query.filter_by(
        booking_code=booking_code,
        shop_id=g.shop.id,
    ).first()
    if not booking:
        flash("We could not find that booking. Check the booking number and try again.", "warning")
        return render_template(
            "booking_lookup.html",
            booking=None,
            booking_code=booking_code,
        )
    phone = request.args.get("phone", "").strip()
    if not phone:
        return render_template(
            "booking_lookup.html",
            booking=None,
            booking_code=booking_code,
        )
    if "".join(ch for ch in phone if ch.isdigit()) != "".join(ch for ch in booking.phone if ch.isdigit()):
        flash("Booking code and mobile number did not match.", "danger")
        return render_template(
            "booking_lookup.html",
            booking=None,
            booking_code=booking_code,
        )
    return render_template(
        "booking_lookup.html",
        booking=booking,
        booking_code=booking_code,
    )


@app.get("/booking/<booking_code>")
def booking_lookup(booking_code):
    return redirect(
        url_for(
            "booking_status_lookup",
            booking_code=booking_code,
            phone=request.args.get("phone", ""),
        )
    )


@app.get("/admin/login")
def admin_login():
    if not g.shop:
        return redirect(url_for("home"))
    next_url = safe_next_url(request.args.get("next"))
    current_owner = db.session.get(Admin, session.get("admin_id")) if session.get("admin_id") else None
    if current_owner and current_owner.shop_id == g.shop.id:
        return redirect(next_url or url_for("admin_dashboard"))
    return render_template(
        "admin/login.html",
        setup_available=not Admin.query.filter_by(shop_id=g.shop.id).first(),
        next_url=next_url,
    )


@app.post("/admin/login")
def admin_login_post():
    if not g.shop:
        return redirect(url_for("home"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    next_url = safe_next_url(request.form.get("next") or request.args.get("next"))
    admin = Admin.query.filter_by(username=username, shop_id=g.shop.id).first()
    if not admin or not admin.check_password(password):
        flash("Invalid username or password.", "danger")
        return render_template(
            "admin/login.html",
            setup_available=not Admin.query.filter_by(shop_id=g.shop.id).first(),
            next_url=next_url,
        ), 401
    session.clear()
    session["admin_id"] = admin.id
    session.permanent = True
    flash("Signed in successfully.", "success")
    return redirect(next_url or url_for("admin_dashboard"))


@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    if not g.shop:
        return redirect(url_for("home"))
    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))
    if Admin.query.filter_by(shop_id=g.shop.id).first():
        flash("The owner account has already been created. Please sign in.", "info")
        return redirect(url_for("admin_login"))
    if request.method == "GET":
        return render_template("admin/setup.html", username="")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not username or len(username) > 80:
        flash("Enter a username between 1 and 80 characters.", "danger")
        return render_template("admin/setup.html", username=username), 400
    if len(password) < 12:
        flash("Use a password with at least 12 characters.", "danger")
        return render_template("admin/setup.html", username=username), 400
    if password != confirm_password:
        flash("The passwords do not match.", "danger")
        return render_template("admin/setup.html", username=username), 400

    admin = Admin(username=username, shop_id=g.shop.id)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    session.clear()
    session["admin_id"] = admin.id
    session.permanent = True
    flash("Owner account created. You are now signed in.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    flash("You have signed out.", "success")
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin_dashboard():
    counts = {
        status: Booking.query.filter_by(shop_id=g.shop.id, status=status).count()
        for status in BOOKING_STATUSES
    }
    product_count = Product.query.filter_by(shop_id=g.shop.id).count()
    low_stock_count = Product.query.filter(
        Product.shop_id == g.shop.id,
        Product.active.is_(True),
        Product.stock <= LOW_STOCK_THRESHOLD,
    ).count()
    low_stock_products = (
        Product.query.filter(
            Product.shop_id == g.shop.id,
            Product.active.is_(True),
            Product.stock <= LOW_STOCK_THRESHOLD,
        )
        .order_by(Product.stock.asc(), Product.name.asc())
        .limit(10)
        .all()
    )
    bookings = Booking.query.filter_by(shop_id=g.shop.id).order_by(Booking.created_at.desc()).limit(50).all()
    return render_template(
        "admin/dashboard.html",
        counts=counts,
        bookings=bookings,
        product_count=product_count,
        low_stock_count=low_stock_count,
        low_stock_products=low_stock_products,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
    )


@app.route("/admin/owners", methods=["GET", "POST"])
@admin_required
def admin_owners():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        owners = Admin.query.filter_by(shop_id=g.shop.id).order_by(Admin.username.asc()).all()
        if not username or len(username) > 80:
            flash("Enter a username between 1 and 80 characters.", "danger")
            return render_template("admin/owners.html", owners=owners, username=username), 400
        if len(password) < 12:
            flash("Use a password with at least 12 characters.", "danger")
            return render_template("admin/owners.html", owners=owners, username=username), 400
        if password != confirm_password:
            flash("The passwords do not match.", "danger")
            return render_template("admin/owners.html", owners=owners, username=username), 400
        if Admin.query.filter_by(username=username).first():
            flash("An owner account with that username already exists.", "danger")
            return render_template("admin/owners.html", owners=owners, username=username), 400

        admin = Admin(username=username, shop_id=g.shop.id)
        admin.set_password(password)
        db.session.add(admin)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An owner account with that username already exists.", "danger")
            return render_template("admin/owners.html", owners=owners, username=username), 400
        flash(f"Owner account '{username}' created. They can now sign in.", "success")
        return redirect(url_for("admin_owners"))

    owners = Admin.query.filter_by(shop_id=g.shop.id).order_by(Admin.username.asc()).all()
    return render_template("admin/owners.html", owners=owners, username="")


@app.route("/admin/account", methods=["GET", "POST"])
@admin_required
def admin_account():
    owner = db.session.get(Admin, session["admin_id"])
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_username = request.form.get("username", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not owner.check_password(current_password):
            flash("Your current password is incorrect.", "danger")
            return render_template("admin/account.html", username=new_username or owner.username), 400

        username_changed = new_username != owner.username
        password_changed = bool(new_password)
        if not username_changed and not password_changed:
            flash("Enter a new username or password to update your account.", "warning")
            return render_template("admin/account.html", username=owner.username), 400

        if not new_username or len(new_username) > 80:
            flash("Enter a username between 1 and 80 characters.", "danger")
            return render_template("admin/account.html", username=new_username), 400
        existing_owner = Admin.query.filter(
            Admin.username == new_username,
            Admin.id != owner.id,
        ).first()
        if existing_owner:
            flash("That username is already in use. Choose another username.", "danger")
            return render_template("admin/account.html", username=new_username), 400
        if password_changed and len(new_password) < 12:
            flash("Use a password with at least 12 characters.", "danger")
            return render_template("admin/account.html", username=new_username), 400
        if password_changed and new_password != confirm_password:
            flash("The new passwords do not match.", "danger")
            return render_template("admin/account.html", username=new_username), 400

        owner.username = new_username
        if password_changed:
            owner.set_password(new_password)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That username is already in use. Choose another username.", "danger")
            return render_template("admin/account.html", username=new_username), 400

        flash("Your owner account was updated.", "success")
        return redirect(url_for("admin_account"))

    return render_template("admin/account.html", username=owner.username)


@app.get("/admin/notifications/bookings")
@admin_required
def admin_booking_notifications():
    after_id = request.args.get("after_id", "0")
    if not after_id.isdigit():
        return jsonify(error="after_id must be a non-negative integer."), 400

    bookings = (
        Booking.query.filter(Booking.shop_id == g.shop.id, Booking.id > int(after_id))
        .order_by(Booking.id.asc())
        .limit(20)
        .all()
    )
    return jsonify(bookings=[
        {
            "id": booking.id,
            "booking_code": booking.booking_code,
            "customer_name": booking.customer_name,
            "phone": booking.phone,
            "pickup_at": booking.pickup_at.strftime("%d %b %Y, %I:%M %p"),
            "payment_method": booking.payment.method if booking.payment else "shop",
            "fulfillment": booking.fulfillment,
            "delivery_fee": booking.delivery_fee,
            "delivery_map_url": (
                f"https://www.google.com/maps?q={booking.delivery_latitude},{booking.delivery_longitude}"
                if booking.fulfillment == "delivery"
                else None
            ),
            "url": url_for("admin_booking_detail", booking_id=booking.id),
            "items": [
                {
                    "name": item.product_name,
                    "quantity": item.quantity,
                    "size": item.size or "Any",
                    "color": item.color or "Any",
                }
                for item in booking.items
            ],
        }
        for booking in bookings
    ])


@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
def admin_products():
    if request.method == "POST":
        product_id = request.form.get("product_id", "").strip()
        product = (
            Product.query.filter_by(id=int(product_id), shop_id=g.shop.id).first()
            if product_id.isdigit()
            else Product(shop_id=g.shop.id)
        )
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()[:3000]
        sizes = request.form.get("sizes", "").strip()[:300]
        colors = request.form.get("colors", "").strip()[:300]
        uploaded_images = [image for image in request.files.getlist("images") if image.filename]
        image_path = product.image_url if product else ""
        existing_image_paths = product_image_paths(product) if product else []
        try:
            price = float(request.form.get("price", ""))
            stock = int(request.form.get("stock", ""))
        except ValueError:
            flash("Price and stock must be valid numbers.", "danger")
            return redirect(url_for("admin_products"))
        if not name or len(name) > 160 or category not in CATEGORIES or price < 0 or price > 10000000 or stock < 0 or stock > 1000000:
            flash("Check product name, category, price and stock values.", "danger")
            return redirect(url_for("admin_products"))
        if product_id.isdigit() and not product:
            abort(404)

        if len(existing_image_paths) + len(uploaded_images) > MAX_PRODUCT_IMAGES:
            flash(f"A product can have up to {MAX_PRODUCT_IMAGES} images total.", "danger")
            return redirect(url_for("admin_products"))

        validated_images = []
        try:
            validated_images = [validate_uploaded_image(image) for image in uploaded_images]
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("admin_products"))

        saved_image_paths = []
        try:
            for extension, image_data in validated_images:
                saved_image_paths.append(save_image_data(image_data, extension, f"{g.shop.slug}/products"))
        except OSError:
            remove_image_files(saved_image_paths)
            app.logger.exception("Unable to save uploaded product images")
            flash("The product images could not be saved. Please try again.", "danger")
            return redirect(url_for("admin_products"))

        for index, saved_path in enumerate(saved_image_paths):
            if not image_path and not existing_image_paths and index == 0:
                image_path = saved_path
            else:
                product.images.append(ProductImage(image_path=saved_path))

        product.name = name
        product.category = category
        product.description = description
        product.image_url = image_path
        product.sizes = sizes
        product.colors = colors
        product.price = price
        product.stock = stock
        product.active = request.form.get("active") == "on"
        db.session.add(product)
        db.session.commit()
        flash("Product saved.", "success")
        return redirect(url_for("admin_products"))

    edit_id = request.args.get("edit", "")
    edit_product = (
        Product.query.filter_by(id=int(edit_id), shop_id=g.shop.id).first()
        if edit_id.isdigit()
        else None
    )
    products_list = Product.query.filter_by(shop_id=g.shop.id).order_by(Product.created_at.desc()).all()
    low_stock_products = [
        product for product in products_list
        if product.active and product.stock <= LOW_STOCK_THRESHOLD
    ]
    edit_product_images = product_image_paths(edit_product) if edit_product else []
    return render_template(
        "admin/products.html",
        products=products_list,
        low_stock_products=low_stock_products,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
        edit_product=edit_product,
        edit_product_images=edit_product_images,
        size_options=SIZE_OPTIONS,
        color_options=COLOR_OPTIONS,
        selected_sizes=(
            [value.strip() for value in edit_product.sizes.split(",") if value.strip()]
            if edit_product and edit_product.sizes
            else []
        ),
        selected_colors=(
            [value.strip() for value in edit_product.colors.split(",") if value.strip()]
            if edit_product and edit_product.colors
            else []
        ),
        max_product_images=MAX_PRODUCT_IMAGES,
        categories=CATEGORIES,
    )


@app.route("/admin/payment-settings", methods=["GET", "POST"])
@admin_required
def admin_payment_settings():
    payment_settings = get_payment_settings()
    if request.method == "POST":
        uploaded_qr = request.files.get("qr_image")
        remove_qr = request.form.get("remove_qr") == "on"
        if uploaded_qr and uploaded_qr.filename:
            try:
                qr_image_path = save_uploaded_image(uploaded_qr, f"{g.shop.slug}/payment")
            except ValueError as error:
                flash(str(error), "danger")
                return redirect(url_for("admin_payment_settings"))
            except OSError:
                app.logger.exception("Unable to save the online payment QR image")
                flash("The payment QR image could not be saved. Please try again.", "danger")
                return redirect(url_for("admin_payment_settings"))
            previous_qr_image_path = (
                payment_settings.qr_image_path if payment_settings else None
            )
            if payment_settings is None:
                payment_settings = PaymentSettings(shop_id=g.shop.id, qr_image_path=qr_image_path)
                db.session.add(payment_settings)
            else:
                payment_settings.qr_image_path = qr_image_path
            db.session.commit()
            if previous_qr_image_path and not remove_uploaded_image_file(previous_qr_image_path):
                flash("QR image updated, but the previous image could not be removed from storage.", "warning")
                return redirect(url_for("admin_payment_settings"))
        elif remove_qr:
            if payment_settings:
                previous_qr_image_path = payment_settings.qr_image_path
                db.session.delete(payment_settings)
                db.session.commit()
                if not remove_uploaded_image_file(previous_qr_image_path):
                    flash("QR image removed from the shop, but its file could not be removed from storage.", "warning")
                    return redirect(url_for("admin_payment_settings"))
        else:
            flash("Choose a QR image to upload or select the option to remove the current one.", "warning")
            return redirect(url_for("admin_payment_settings"))

        if uploaded_qr and uploaded_qr.filename:
            flash("Online payment QR settings saved.", "success")
            return redirect(url_for("admin_payment_settings"))
        db.session.commit()
        flash("Online payment QR settings saved.", "success")
        return redirect(url_for("admin_payment_settings"))

    return render_template("admin/payment_settings.html", payment_settings=payment_settings)


@app.route("/admin/shop-appearance", methods=["GET", "POST"])
@admin_required
def admin_shop_appearance():
    storefront_settings = get_storefront_settings()
    if request.method == "POST":
        uploaded_images = [
            image for image in request.files.getlist("shop_images") if image.filename
        ]
        remove_image = request.form.get("remove_image") == "on"
        if uploaded_images and remove_image:
            flash("Upload new photos or remove the current gallery, but not both at once.", "warning")
            return redirect(url_for("admin_shop_appearance"))

        if uploaded_images:
            current_images = storefront_image_paths(storefront_settings)
            if len(current_images) + len(uploaded_images) > MAX_PRODUCT_IMAGES:
                flash(f"The home page can show up to {MAX_PRODUCT_IMAGES} photos.", "danger")
                return redirect(url_for("admin_shop_appearance"))

            try:
                validated_images = [
                    validate_uploaded_image(image) for image in uploaded_images
                ]
            except ValueError as error:
                flash(str(error), "danger")
                return redirect(url_for("admin_shop_appearance"))

            saved_image_paths = []
            try:
                for extension, image_data in validated_images:
                    saved_image_paths.append(
                        save_image_data(image_data, extension, f"{g.shop.slug}/storefront")
                    )
            except OSError:
                remove_image_files(saved_image_paths)
                app.logger.exception("Unable to save the storefront image")
                flash("The shop front image could not be saved. Please try again.", "danger")
                return redirect(url_for("admin_shop_appearance"))

            if storefront_settings is None:
                storefront_settings = StorefrontSettings(
                    shop_id=g.shop.id,
                    shop_image_path=saved_image_paths.pop(0),
                )
                db.session.add(storefront_settings)
            storefront_settings.images.extend(
                StorefrontImage(image_path=image_path)
                for image_path in saved_image_paths
            )
        elif remove_image:
            if storefront_settings:
                removed_image_paths = storefront_image_paths(storefront_settings)
                db.session.delete(storefront_settings)
                db.session.commit()
                if not remove_image_files(removed_image_paths):
                    flash("Shop photos were removed, but one or more files could not be removed from storage.", "warning")
                    return redirect(url_for("admin_shop_appearance"))
        else:
            flash("No photo selected. Your current shop photos have not been changed.", "info")
            return redirect(url_for("admin_shop_appearance"))

        if uploaded_images:
            db.session.commit()
        flash("Shop appearance saved.", "success")
        return redirect(url_for("admin_shop_appearance"))

    return render_template(
        "admin/shop_appearance.html",
        storefront_settings=storefront_settings,
        shop_images=storefront_image_paths(storefront_settings),
        shop_photos=(
            [{"id": None, "image_path": storefront_settings.shop_image_path}]
            + [
                {"id": image.id, "image_path": image.image_path}
                for image in storefront_settings.images
            ]
            if storefront_settings
            else []
        ),
        max_product_images=MAX_PRODUCT_IMAGES,
    )


@app.post("/admin/shop-location")
@admin_required
def admin_shop_location():
    if request.form.get("location_action") == "toggle_delivery":
        requested_state = request.form.get("delivery_enabled") == "on"
        if requested_state and (
            g.shop.latitude is None or g.shop.longitude is None
        ):
            flash("Save the shop location before enabling delivery.", "warning")
            return redirect(url_for("admin_shop_appearance"))
        g.shop.delivery_enabled = requested_state
        db.session.commit()
        flash(
            "Cash-on-delivery requests enabled."
            if requested_state
            else "Cash-on-delivery requests disabled.",
            "success",
        )
        return redirect(url_for("admin_shop_appearance"))

    if request.form.get("location_action") == "clear":
        g.shop.delivery_enabled = False
        g.shop.latitude = None
        g.shop.longitude = None
        db.session.commit()
        flash("Shop location cleared and delivery disabled.", "success")
        return redirect(url_for("admin_shop_appearance"))

    try:
        latitude = float(request.form.get("latitude", ""))
        longitude = float(request.form.get("longitude", ""))
    except (TypeError, ValueError):
        flash("Share the shop's current location before saving.", "danger")
        return redirect(url_for("admin_shop_appearance"))
    if not coordinates_are_valid(latitude, longitude):
        flash("The shared shop location is not valid. Please share it again.", "danger")
        return redirect(url_for("admin_shop_appearance"))

    g.shop.latitude = latitude
    g.shop.longitude = longitude
    db.session.commit()
    flash("Shop delivery location saved.", "success")
    return redirect(url_for("admin_shop_appearance"))


@app.post("/admin/shop-appearance/images/primary/delete")
@admin_required
def delete_primary_storefront_image():
    storefront_settings = get_storefront_settings()
    if storefront_settings is None:
        abort(404)

    image_path = storefront_settings.shop_image_path
    if storefront_settings.images:
        replacement = storefront_settings.images[0]
        storefront_settings.shop_image_path = replacement.image_path
        db.session.delete(replacement)
    else:
        db.session.delete(storefront_settings)

    db.session.commit()
    if remove_uploaded_image_file(image_path):
        flash("Shop photo deleted.", "success")
    else:
        flash("The photo was removed from the shop, but its file could not be deleted.", "warning")
    return redirect(url_for("admin_shop_appearance"))


@app.post("/admin/shop-appearance/images/<int:image_id>/delete")
@admin_required
def delete_storefront_gallery_image(image_id):
    image = (
        StorefrontImage.query
        .join(StorefrontSettings)
        .filter(StorefrontImage.id == image_id, StorefrontSettings.shop_id == g.shop.id)
        .first_or_404()
    )
    image_path = image.image_path
    db.session.delete(image)
    db.session.commit()
    if remove_uploaded_image_file(image_path):
        flash("Shop photo deleted.", "success")
    else:
        flash("The photo was removed from the shop, but its file could not be deleted.", "warning")
    return redirect(url_for("admin_shop_appearance"))


@app.post("/admin/products/<int:product_id>/toggle")
@admin_required
def toggle_product(product_id):
    product = Product.query.filter_by(id=product_id, shop_id=g.shop.id).first_or_404()
    product.active = not product.active
    db.session.commit()
    flash(f"{product.name} is now {'visible' if product.active else 'hidden'} in the store.", "success")
    return redirect(url_for("admin_products"))


@app.post("/admin/products/<int:product_id>/delete")
@admin_required
def delete_product(product_id):
    product = Product.query.filter_by(id=product_id, shop_id=g.shop.id).first_or_404()
    if BookingItem.query.filter_by(product_id=product.id).first():
        product.active = False
        db.session.commit()
        flash("This product is part of booking history, so it was hidden instead of permanently deleted.", "warning")
    else:
        removed_image_paths = product_image_paths(product)
        db.session.delete(product)
        db.session.commit()
        if not remove_image_files(removed_image_paths):
            flash("Product deleted, but one or more image files could not be removed from storage.", "warning")
            return redirect(url_for("admin_products"))
        flash("Product deleted.", "success")
    return redirect(url_for("admin_products"))


@app.get("/admin/bookings")
@admin_required
def admin_bookings():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    query = Booking.query.filter_by(shop_id=g.shop.id)
    if status in BOOKING_STATUSES:
        query = query.filter_by(status=status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Booking.customer_name.ilike(like), Booking.phone.ilike(like), Booking.booking_code.ilike(like)))
    bookings = query.order_by(Booking.created_at.desc()).all()
    return render_template("admin/bookings.html", bookings=bookings, statuses=BOOKING_STATUSES, selected_status=status, q=q)


@app.route("/admin/bookings/<int:booking_id>", methods=["GET", "POST"])
@admin_required
def admin_booking_detail(booking_id):
    booking = Booking.query.filter_by(id=booking_id, shop_id=g.shop.id).first_or_404()
    if request.method == "POST":
        if request.form.get("send_whatsapp_update") == "1":
            if not booking.whatsapp_updates_opt_in:
                flash(
                    "This customer did not opt in to WhatsApp updates.",
                    "warning",
                )
            elif send_whatsapp_status_notification(booking, g.shop.name):
                flash("WhatsApp status update sent.", "success")
            else:
                flash(
                    "The WhatsApp update could not be sent. Check the WhatsApp "
                    "API settings and provider logs.",
                    "warning",
                )
            return redirect(url_for("admin_booking_detail", booking_id=booking.id))

        new_status = request.form.get("status", "")
        if new_status not in BOOKING_STATUSES:
            flash("Invalid booking status.", "danger")
            return redirect(url_for("admin_booking_detail", booking_id=booking.id))
        if booking.status == "Collected" and new_status != "Collected":
            flash("Collected bookings cannot be reopened. Create a new booking if needed.", "warning")
            return redirect(url_for("admin_booking_detail", booking_id=booking.id))
        if booking.status == "Cancelled" and new_status != "Cancelled":
            flash("Cancelled bookings cannot be reopened. Ask the customer to book again.", "warning")
            return redirect(url_for("admin_booking_detail", booking_id=booking.id))
        if booking.status == "Rejected" and new_status != "Rejected":
            flash("Rejected bookings cannot be reopened. Ask the customer to book again.", "warning")
            return redirect(url_for("admin_booking_detail", booking_id=booking.id))
        if new_status in {"Cancelled", "Rejected"} and booking.status not in {"Cancelled", "Rejected"}:
            for item in booking.items:
                product = Product.query.filter_by(
                    id=item.product_id,
                    shop_id=g.shop.id,
                ).first()
                if product:
                    product.stock += item.quantity
        status_changed = booking.status != new_status
        booking.status = new_status
        db.session.commit()
        if status_changed:
            flash("Booking status updated.", "success")
            if booking.whatsapp_updates_opt_in:
                if not send_whatsapp_status_notification(booking, g.shop.name):
                    flash(
                        "The booking status was saved, but its WhatsApp update "
                        "could not be sent. Check the WhatsApp API settings and "
                        "provider logs. Use the retry button below to send it "
                        "again after fixing the issue.",
                        "warning",
                    )
        else:
            flash("Booking status was unchanged.", "info")
        return redirect(url_for("admin_booking_detail", booking_id=booking.id))
    items_subtotal = sum(
        (item.unit_price * item.quantity for item in booking.items),
        start=0,
    )
    return render_template(
        "admin/booking_detail.html",
        booking=booking,
        statuses=BOOKING_STATUSES,
        items_subtotal=items_subtotal,
    )


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.cli.command("init-db")
def init_db():
    """Create tables and attach existing single-shop data to its legacy shop."""
    Shop.__table__.create(db.engine, checkfirst=True)
    shop_columns = {
        column["name"] for column in inspect(db.engine).get_columns("shop")
    }
    shop_column_definitions = {
        "latitude": "REAL",
        "longitude": "REAL",
        "delivery_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    for column_name, definition in shop_column_definitions.items():
        if column_name not in shop_columns:
            db.session.execute(
                text(f"ALTER TABLE shop ADD COLUMN {column_name} {definition}")
            )
    db.session.commit()

    default_shop = Shop.query.filter_by(slug="local-fashion-store").first()
    if default_shop is None:
        default_shop = Shop(
            name=os.environ.get("SHOP_NAME", "Local Fashion Store"),
            slug="local-fashion-store",
        )
        db.session.add(default_shop)
        db.session.commit()

    inspector = inspect(db.engine)
    for table_name in ("admin", "product", "booking", "payment_settings", "storefront_settings"):
        if table_name in inspector.get_table_names() and "shop_id" not in {
            column["name"] for column in inspector.get_columns(table_name)
        }:
            db.session.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN shop_id INTEGER REFERENCES shop(id)")
            )
            db.session.commit()

    db.create_all()
    delivery_columns = {
        "shop": {
            "latitude": "REAL",
            "longitude": "REAL",
            "delivery_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "booking": {
            "fulfillment": "VARCHAR(20) NOT NULL DEFAULT 'pickup'",
            "whatsapp_updates_opt_in": "BOOLEAN NOT NULL DEFAULT FALSE",
            "whatsapp_opted_in_at": "TIMESTAMP",
            "delivery_latitude": "REAL",
            "delivery_longitude": "REAL",
            "delivery_distance_km": "REAL",
            "delivery_distance_method": (
                "VARCHAR(20) NOT NULL DEFAULT 'straight_line'"
            ),
            "delivery_fee": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table_name, columns in delivery_columns.items():
        existing_columns = {
            column["name"] for column in inspect(db.engine).get_columns(table_name)
        }
        for column_name, definition in columns.items():
            if column_name not in existing_columns:
                db.session.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN "
                        f"{column_name} {definition}"
                    )
                )
    db.session.commit()

    for table_name in ("admin", "product", "booking", "payment_settings", "storefront_settings"):
        db.session.execute(
            text(f"UPDATE {table_name} SET shop_id = :shop_id WHERE shop_id IS NULL"),
            {"shop_id": default_shop.id},
        )
    db.session.commit()
    click.echo("Database tables created.")


@app.cli.command("create-admin")
@click.option("--username", prompt=True)
@click.password_option()
@click.option("--shop-slug", default="local-fashion-store", help="Shop that owns this account.")
def create_admin(username, password, shop_slug):
    """Create or reset an owner account."""
    if len(password) < 12:
        raise click.ClickException("Use a password with at least 12 characters.")
    shop = Shop.query.filter_by(slug=shop_slug).first()
    if not shop:
        raise click.ClickException(f"Shop '{shop_slug}' does not exist.")
    admin = Admin.query.filter_by(username=username, shop_id=shop.id).first()
    if not admin and Admin.query.filter_by(username=username).first():
        raise click.ClickException("That username is already used by an owner of another shop.")
    if not admin:
        admin = Admin(username=username, shop_id=shop.id)
        db.session.add(admin)
    admin.set_password(password)
    db.session.commit()
    click.echo(f"Admin account '{username}' is ready.")


@app.cli.command("seed-demo")
def seed_demo():
    """Add sample products to the legacy shop if its catalog is empty."""
    shop = Shop.query.filter_by(slug="local-fashion-store").first()
    if not shop:
        raise click.ClickException("Run 'flask --app app init-db' first.")
    if Product.query.filter_by(shop_id=shop.id).first():
        click.echo("Products already exist; no sample products added.")
        return
    samples = [
        ("Classic Cotton Shirt", "Men's Clothing", "Everyday cotton shirt. Replace this sample with your real products.", 799, "S, M, L, XL", "Black, White, Blue, Navy, Grey, Green", 10),
        ("Women's Printed Kurti", "Women's Clothing", "Comfortable printed kurti for daily wear.", 699, "S, M, L, XL", "Pink, Red, Blue, Green, Yellow, Purple, Black, White", 8),
        ("Casual Sneakers", "Shoes", "Casual shoes. Select size when booking.", 1299, "6, 7, 8, 9, 10", "Black, White, Blue, Grey, Red", 6),
        ("Daily Wear Chappals", "Chappals & Sandals", "Simple everyday chappals.", 299, "6, 7, 8, 9, 10", "Black, Brown, Tan, Blue, Grey", 12),
    ]
    for name, category, description, price, sizes, colors, stock in samples:
        db.session.add(Product(shop_id=shop.id, name=name, category=category, description=description, price=price,
                               sizes=sizes, colors=colors, stock=stock, active=True))
    db.session.commit()
    click.echo("Sample products added. Update or remove them before going live.")


with app.app_context():
    # Tables are intentionally created with the CLI command, not automatically on every production boot.
    pass


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")

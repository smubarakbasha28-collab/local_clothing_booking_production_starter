import os
import getpass
from pathlib import Path
from uuid import uuid4

import click
import cloudinary
import cloudinary.uploader
from sqlalchemy import MetaData, create_engine, inspect, select, text, update

application = None


TABLE_ORDER = (
    "shop",
    "admin",
    "product",
    "booking",
    "payment_settings",
    "storefront_settings",
    "product_image",
    "booking_payment",
    "storefront_image",
    "booking_item",
)
IMAGE_COLUMNS = {
    ("product", "image_url"),
    ("product_image", "image_path"),
    ("payment_settings", "qr_image_path"),
    ("storefront_settings", "shop_image_path"),
    ("storefront_image", "image_path"),
}


def local_image_file(image_path):
    if not image_path.startswith("uploads/"):
        return None

    upload_root = Path(application.app.static_folder, "uploads").resolve()
    image_file = (Path(application.app.static_folder) / image_path).resolve()
    try:
        image_file.relative_to(upload_root)
    except ValueError as error:
        raise click.ClickException(
            f"Refusing to migrate an image outside the uploads directory: {image_path}"
        ) from error
    if not image_file.is_file():
        raise click.ClickException(f"Uploaded image file is missing: {image_path}")
    return image_file


def migrate_database(source_engine, target_engine, upload_image):
    source_inspector = inspect(source_engine)
    target_inspector = inspect(target_engine)
    source_tables = set(source_inspector.get_table_names())
    target_tables = set(target_inspector.get_table_names())
    missing_tables = set(TABLE_ORDER) - source_tables
    if missing_tables:
        raise click.ClickException(
            "The source database is missing expected tables: "
            + ", ".join(sorted(missing_tables))
        )
    missing_tables = set(TABLE_ORDER) - target_tables
    if missing_tables:
        raise click.ClickException(
            "The destination database is missing tables. Run "
            "'flask --app app init-db' against DATABASE_URL first. Missing: "
            + ", ".join(sorted(missing_tables))
        )

    metadata = MetaData()
    metadata.reflect(bind=source_engine, only=TABLE_ORDER)
    tables = {
        table_name: application.db.metadata.tables[table_name]
        for table_name in TABLE_ORDER
    }

    source_rows = {}
    for table_name in TABLE_ORDER:
        source_table = metadata.tables[table_name]
        with source_engine.connect() as connection:
            source_rows[table_name] = [
                dict(row)
                for row in connection.execute(select(source_table)).mappings()
            ]

    destination_shop = tables["shop"]
    with target_engine.connect() as connection:
        existing_shops = {
            row["id"]: row
            for row in connection.execute(select(destination_shop)).mappings()
        }
        for table_name in TABLE_ORDER:
            if table_name == "shop":
                continue
            table = tables[table_name]
            existing_count = connection.execute(
                select(text("count(*)")).select_from(table)
            ).scalar_one()
            if existing_count:
                raise click.ClickException(
                    f"The destination already contains {existing_count} rows in "
                    f"'{table_name}'. Refusing to merge or overwrite live data."
                )

    source_shops_by_id = {
        row["id"]: row for row in source_rows["shop"]
    }
    source_shop_ids_by_slug = {
        row["slug"]: row["id"] for row in source_rows["shop"]
    }
    for shop_id, row in existing_shops.items():
        if (
            shop_id not in source_shops_by_id
            or source_shops_by_id[shop_id]["slug"] != row["slug"]
        ):
            raise click.ClickException(
                "The destination has a shop that does not match the source "
                "database. Refusing to overwrite it."
            )

    for source_shop in source_rows["shop"]:
        matching_id = source_shop_ids_by_slug.get(source_shop["slug"])
        if matching_id != source_shop["id"]:
            raise click.ClickException(
                f"Shop ID mismatch for '{source_shop['slug']}'."
            )

    image_paths = set()
    for table_name, column_name in IMAGE_COLUMNS:
        image_paths.update(
            row[column_name]
            for row in source_rows[table_name]
            if row.get(column_name)
        )
    for image_path in image_paths:
        local_image_file(image_path)

    migrated_image_paths = {}
    for image_path in sorted(image_paths):
        if image_path.startswith("uploads/"):
            migrated_image_paths[image_path] = upload_image(
                local_image_file(image_path)
            )
        elif image_path.startswith("cloudinary:"):
            migrated_image_paths[image_path] = image_path
        else:
            migrated_image_paths[image_path] = image_path

    inserted_counts = {}
    with target_engine.begin() as connection:
        for table_name in TABLE_ORDER:
            table = tables[table_name]
            target_column_names = {column.name for column in table.columns}
            rows = []
            for source_row in source_rows[table_name]:
                row = {
                    key: value
                    for key, value in source_row.items()
                    if key in target_column_names
                }
                for column_name in target_column_names:
                    if (table_name, column_name) in IMAGE_COLUMNS and row.get(column_name):
                        row[column_name] = migrated_image_paths[row[column_name]]
                rows.append(row)

            existing_shop_ids = set(existing_shops)
            new_rows = []
            for row in rows:
                if table_name == "shop" and row["id"] in existing_shop_ids:
                    values = {key: value for key, value in row.items() if key != "id"}
                    connection.execute(
                        update(table).where(table.c.id == row["id"]).values(**values)
                    )
                else:
                    new_rows.append(row)
            if new_rows:
                connection.execute(table.insert(), new_rows)
            inserted_counts[table_name] = len(new_rows)

        if target_engine.dialect.name == "postgresql":
            for table_name in TABLE_ORDER:
                table = tables[table_name]
                if "id" not in table.c:
                    continue
                max_id = connection.execute(select(text("max(id)")).select_from(table)).scalar_one()
                if max_id is None:
                    continue
                sequence = connection.execute(
                    text(
                        "SELECT pg_get_serial_sequence(:table_name, 'id')"
                    ),
                    {"table_name": table_name},
                ).scalar_one()
                if sequence:
                    connection.execute(
                        text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                        {"sequence": sequence, "value": max_id},
                    )

    return inserted_counts


def cloudinary_image_uploader():
    if not os.environ.get("CLOUDINARY_URL"):
        raise click.ClickException(
            "Set CLOUDINARY_URL in your local shell before migrating photos."
        )
    cloudinary.config(secure=True)

    def upload_image(image_file):
        public_id = f"migration/{uuid4().hex}"
        result = cloudinary.uploader.upload(
            str(image_file),
            public_id=public_id,
            overwrite=False,
            resource_type="image",
        )
        returned_public_id = result.get("public_id")
        if not returned_public_id:
            raise click.ClickException(
                f"Cloudinary returned no public ID for {image_file.name}."
            )
        return f"cloudinary:{returned_public_id}"

    return upload_image


@click.command()
@click.option(
    "--source-db",
    default="sqlite:///instance/shop.db",
    show_default=True,
    help="SQLAlchemy URL for the current local database.",
)
def main(source_db):
    database_url = os.environ.get("DATABASE_URL") or getpass.getpass(
        "Neon PostgreSQL DATABASE_URL (input hidden): "
    )
    if not database_url:
        raise click.ClickException("DATABASE_URL is required.")
    os.environ["DATABASE_URL"] = database_url

    cloudinary_url = os.environ.get("CLOUDINARY_URL") or getpass.getpass(
        "Cloudinary CLOUDINARY_URL (input hidden): "
    )
    if not cloudinary_url:
        raise click.ClickException("CLOUDINARY_URL is required.")
    os.environ["CLOUDINARY_URL"] = cloudinary_url

    global application
    import app as application

    destination_url = application.app.config["SQLALCHEMY_DATABASE_URI"]
    if not destination_url.startswith("postgresql"):
        raise click.ClickException(
            "Set DATABASE_URL to your Neon PostgreSQL connection string locally. "
            "Do not point this migration at the local SQLite database."
        )
    if destination_url == source_db:
        raise click.ClickException("Source and destination databases must differ.")

    source_engine = create_engine(source_db, pool_pre_ping=True)
    try:
        with application.app.app_context():
            counts = migrate_database(
                source_engine,
                application.db.engine,
                cloudinary_image_uploader(),
            )
    finally:
        source_engine.dispose()

    click.echo("Migration completed:")
    for table_name, count in counts.items():
        click.echo(f"  {table_name}: {count} new rows")
    click.echo("Uploaded images are stored in Cloudinary.")


if __name__ == "__main__":
    main()

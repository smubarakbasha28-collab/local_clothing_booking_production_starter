# Local Fashion Store — Reserve Online, Pick Up In Store

A Flask marketplace for independent local clothing and footwear shops. Each shop has its own storefront URL, catalog, bookings, payment QR and appearance settings. Shop owners can create a shop account from the home page, then sign in to manage only their own shop. Customers can browse and reserve items before visiting. There is **no delivery workflow or automatic payment processing**.

## Features

- Public product catalog, search, category filters and product details
- Men's clothing, women's clothing, shoes, chappals/sandals and accessories
- Customer booking with name, mobile number, expected pickup time, size/color and quantities; customers may opt in to WhatsApp status updates
- Optional manual UPI payment using a shop QR code uploaded by the owner; customers can still choose to pay at the shop
- Stock is reserved when a booking is created and returned if the owner rejects or cancels it
- Owner dashboard, product add/edit/hide/delete, booking search and status management
- Boutique typography using DM Sans and Playfair Display, with animated product flip cards on desktop and touch controls on mobile
- Booking statuses: New (awaiting owner decision), Accepted, Preparing, Ready for Pickup, Collected, Rejected, Cancelled
- Public booking lookup requires both booking code and the phone number
- CSRF protection, password hashing, session cookie settings, input validation and health endpoint
- PostgreSQL support for production and SQLite for local development

## Important before real use

This is a starter application, not a formally audited commerce system. Test it with sample data before taking live bookings. WhatsApp status updates are optional and require a Meta WhatsApp Cloud API account, customer opt-in, and an approved message template; without this configuration, bookings and status changes still work normally. The app does not process payments automatically. Online UPI transfers are made outside the site and must be verified manually by the owner; an online payment choice is not proof of payment. Do not collect more personal data than needed. Restrict the admin account, use a strong unique password, configure backups, and verify the hosting/database provider's retention and access settings.

Bookings reserve stock immediately. Owners can accept an order to begin preparation or reject it to release the reserved stock. The owner should cancel expired/uncollected accepted orders to release stock. Expiry automation is not included.

## Portable self-hosting with Docker Compose

The app can run on Windows, macOS, or Linux anywhere Docker Engine and the Docker Compose plugin are available. The included Compose setup uses open-source containers, SQLite, and two persistent Docker volumes: `fashion_database` stores `/data/shop.db`, and `fashion_uploads` stores uploaded shop, product, and payment images. It runs a single Gunicorn worker because this SQLite-and-local-filesystem setup is intended for one app instance.

1. Install Docker Desktop (Windows/macOS) or Docker Engine and the Compose plugin (Linux).
2. Copy `.env.example` to `.env` and set `SECRET_KEY` to a new random value. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep `.env` private; it is excluded from Git.
3. Build and start the site:

   ```sh
   docker compose up --build -d
   ```

   On first start, the app creates or migrates the database. Open `http://localhost:8000/` and create the first owner account from the site.
4. To stop the app without deleting its data, run `docker compose down`. To start it again, run `docker compose up -d`.

Back up both Docker volumes before moving to another machine or making risky changes. `docker compose down -v` permanently deletes both volumes and all stored shop data/uploads. The volumes persist on the current Docker host; they are not automatically copied between hosts.

The database and uploaded images from your current local installation are not copied automatically. Migrate them into the Docker volumes before using the deployment if you need to keep existing shop data.

For public internet access, run this on an internet-reachable server and put an HTTPS reverse proxy and domain in front of the app. Set `COOKIE_SECURE=true` in `.env` when HTTPS is configured. This SQLite and local-volume deployment supports one app instance; use PostgreSQL and shared object storage for multi-instance scaling. Docker makes the deployment portable, but does not provide a server, domain, backups, or public availability by itself.

## Free demo deployment on Render

The included `render.yaml` uses Render's free web-service plan, which provides a free `*.onrender.com` address and HTTPS. For data that survives app restarts, create a free Neon PostgreSQL database and a Cloudinary account for uploaded images, then provide their connection values as `DATABASE_URL` and `CLOUDINARY_URL` when Render prompts for Blueprint environment variables. The database URL should use SSL; the app supports PostgreSQL through psycopg 3. Existing local SQLite records and uploaded images are not migrated automatically.

To enable customer WhatsApp status updates, configure `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_TEMPLATE_NAME` as private environment variables on the hosting service. `WHATSAPP_TEMPLATE_LANGUAGE` defaults to `en`, and `WHATSAPP_API_VERSION` defaults to `v23.0`. Create and get approval for a WhatsApp utility template with four body parameters in this order: customer name, booking code, new status, and shop name (for example: `Hi {{1}}, booking {{2}} at {{4}} is now {{3}}.`). Customers must opt in on the booking form and enter their mobile number with its country code. The site logs delivery failures and shows the owner a warning; a failed send does not undo the saved status change. Owners can retry an opted-in customer's current status update from the booking detail page.

Render's free web service can sleep when idle, its local filesystem is temporary, and its free PostgreSQL product expires after 30 days. This deployment therefore uses Neon and Cloudinary for external data storage; the service may take about a minute to wake from sleep. Free-tier quotas, terms, and availability are controlled by those providers and may change. This is suitable for a demo, not a guaranteed always-on production storefront.

1. Push this project to a GitHub repository, then connect that repository in Render.
2. Create the free Neon database and copy its pooled PostgreSQL connection string. Create a Cloudinary account and copy its product environment URL. Enter both as the `DATABASE_URL` and `CLOUDINARY_URL` secrets requested by Render; never commit them to Git.
3. In Render, choose **New > Blueprint**, connect the repository and deploy `render.yaml`. The start command runs the idempotent database initializer before Gunicorn. Render creates the free HTTPS `*.onrender.com` URL after the first deploy.
4. Open the new URL and create the owner/shop account. Do not run the demo seed command on a real catalog.

The app stores Cloudinary assets by public ID and continues to support local `uploads/` paths for local development. Newly uploaded product, storefront, and payment images use Cloudinary when `CLOUDINARY_URL` is configured. Existing local photo files must be migrated separately before switching an existing shop to this deployment.

### Migrate an existing local shop to Neon and Cloudinary

After deploying the latest code and confirming Render is **Live**, run this one-time migration from the project folder on the computer that has the original `instance/shop.db` and `static/uploads` files:

```powershell
git pull
.\.venv\Scripts\python.exe migrate_existing_data.py
```

The script prompts for the Neon PostgreSQL connection string and Cloudinary environment URL without showing the characters as you type. It copies the shops, owner password hashes, products, bookings, payment settings, and referenced uploaded images. It preserves IDs so existing relationships and passwords remain valid. The destination may contain only the empty default shop created by `init-db`; the script stops rather than overwrite a destination containing other records. Run it only once. If it reports an error, do not retry against a partially populated destination; inspect the error and destination first.

The migration does not change the local SQLite file or delete local images. Back up `instance/shop.db` and `static/uploads` before starting. Do not paste the Neon or Cloudinary values into chat or commit them to Git.

## Run locally (Windows)

1. Install Python 3.11 or 3.12.
2. Extract the ZIP and open Command Prompt/PowerShell in the project folder.
3. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   ```

4. Install packages:

   ```powershell
   pip install -r requirements.txt
   ```

5. Set a development secret and local database (PowerShell):

   ```powershell
   $env:SECRET_KEY = "change-this-to-a-long-random-development-secret"
   $env:DATABASE_URL = "sqlite:///shop.db"
   $env:COOKIE_SECURE = "false"
   $env:SHOP_NAME = "Local Fashion Store"
   ```

6. Create the database and add sample products:

   ```powershell
   flask --app app init-db
   flask --app app seed-demo
   ```

   Start the app and open `/`. Use **Create a shop owner account** to register a shop and its first owner. The shop receives its own link (for example `/shop/your-shop-name/`). Use a unique password of at least 12 characters. To create or reset an owner account later, run `flask --app app create-admin --shop-slug your-shop-name`. Sample products are only seeded into the legacy `local-fashion-store`; update or delete them.

7. Start the app:

   ```powershell
   flask --app app run --debug
   ```

8. Visit `http://127.0.0.1:5000`. Create a shop from the home-page signup link, or visit an existing shop's `/shop/<shop-slug>/admin/login` page.

### Product images

Upload up to 10 JPG, PNG, GIF or WEBP images per product from the Products & stock form, with no per-image size limit. New images append to that product's gallery; the customer product page shows a main image with selectable thumbnails, and catalog cards use the first image. A single upload request is limited to 100 MB total. Select product sizes and colors from the owner-form dropdowns, or enter custom values. Images use Cloudinary when `CLOUDINARY_URL` is configured; otherwise they are saved locally under `static/uploads`.

### Optional UPI payments

From the owner dashboard, open **Payment settings** and upload the shop's UPI QR image. Customers can choose online payment and scan it with PhonePe, Google Pay, Paytm or another UPI app, or choose to pay at the shop. Bookings record the customer's preference for the owner. The site does not process or confirm transfers; verify payments independently before marking an order paid or handing over goods. QR images use the same format validation and cloud/local storage behavior as product images.

### Delivery orders

In-store pickup reservations remain available. Delivery is opt-in and off by default. To accept cash-on-delivery orders, an owner opens **Shop appearance**, uses **Use this device's location**, saves the shop pin, and switches on **Enable home delivery and cash on delivery**. Customers can then choose home delivery and share their current phone location. The app requests a driving route from the OpenStreetMap OSRM routing service and charges ₹5 for each road kilometre, rounding up to the next whole kilometre (for example, 2.3 km estimates to ₹15). The route service receives both shop and customer coordinates; route availability and map coverage can vary, so owners should confirm the route and final fee with the customer before dispatch. The public OSRM demo service is best-effort and has no uptime guarantee; use a self-hosted or supported routing provider for higher-volume production. Existing bookings retain their original straight-line distance and fee; new delivery requests use the road-route calculation. Location sharing requires browser permission and a secure HTTPS page.

### Home page shop photo

From the owner dashboard, open **Shop appearance** to upload up to 10 shop front or interior photos for the home page slideshow. JPG, PNG, GIF and WEBP images with no per-image size limit are supported; later uploads append to the slideshow. Photos rotate automatically, with pause, next/previous and slide controls. The homepage scales each photo to fit the slideshow without cropping it. Use **Delete photo** below an individual image to remove it; deleting the primary photo promotes the next one. Removing the entire gallery restores the default artwork. Photos use Cloudinary when configured and otherwise require persistent app-server storage.

## Admin workflow

1. Each shop owner signs in at `/shop/<shop-slug>/admin/login`. Use **Owner logout** in the top navigation to sign out.
2. Each owner and customer uses that shop's own link, such as `/shop/your-shop-name/`. To add another owner to the same shop, sign in and use **Create owner account** in the owner portal. A new business creates its own shop from **Create a shop owner account** on the directory home page.
3. An owner can change their own username or password from **My account** in the owner portal; the current password is required, and new passwords must contain at least 12 characters.
4. Add real products and enter actual available stock.
5. Keep an owner portal page open to receive live in-page alerts for new reservations. Owners can opt in to browser notifications for alerts while another tab is active; the browser may require notification permission. The owner portal checks for new bookings every 10 seconds. These owner alerts are not sent when the owner portal is closed. Customer WhatsApp alerts require the separate Cloud API configuration described above; SMS and email notifications are not supported.
6. Review `/admin/bookings` when customers reserve items.
7. Open a new booking and accept or reject it. Accepting begins the order; rejecting returns its reserved stock.
8. Move accepted orders to `Preparing`, then `Ready for Pickup`.
9. Mark it `Collected` only after the customer visits and completes the purchase.
10. Mark no-shows as `Cancelled` to release reserved stock.

## Routes

- `/` — directory of shops
- `/owner-signup` — create a new shop and its first owner account
- `/shop/<shop-slug>/` — individual shop storefront (all catalog, booking and owner routes are under this path)
- `/shop/<shop-slug>/admin/login` — sign in to a specific shop
- `/health` — health check

## Data backup

Back up PostgreSQL regularly and test restoring a backup. Keep an offline copy of essential booking records if this is used for real shop operations.

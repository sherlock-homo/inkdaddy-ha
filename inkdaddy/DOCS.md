# Using inkDaddy

1. Pair the display in the Home Assistant Companion app. Scan the Matter QR code on the display.
2. Open the inkDaddy app and choose **Devices → Connect a display**. Select the display and choose **Connect display**, or enter the temporary sharing code from the display’s Matter device page.
3. Add photos and create dashboards. Pick a layout and add widgets for recent Home Assistant sensor readings, weather, the local date, location, or the display’s battery. Use **Preview** to check how a dashboard will look on the chosen display.
4. Open **Displays → Edit playlist** to choose what rotates on the frame. The default interval is 60 minutes. To change it, open **Devices**, enter a new interval for the frame, and select **Save schedule**. The Displays tab shows the last confirmed frame and the next five scheduled slots.

Open **Integrations → Home Assistant settings** to manage the connection and browse entities. For connection troubleshooting, open **Settings → Diagnostics**.

When upgrading from dev.19 or earlier, keep an app backup with that version. The upgrade converts stored playback times from hours to minutes. To return to the earlier version, restore its matching app backup.

## Updates

Open **Settings → Updates**. Choose **Update in Home Assistant** when a release is available, or **Manage app in Home Assistant** to change automatic updates. With remote administrator access to that Home Assistant installation, you can do this away from home. Each installation controls its own updates.

The same tab lists the firmware versions Home Assistant has recorded for registered inkDaddy displays. Select a display to open its native device page. Its Matter controller handles signed firmware updates over Thread.

## Dashboard design and weather

The dashboard grid covers the entire frame. The starter places Date, Current weather and Battery in the top row. The seven-day forecast fills the bottom row. Select a tile to move it or drag an edge to resize it. The battery graphic fills to the reported percentage. The current weather tile uses your selected Home Assistant weather entity.

In **Dashboards**, open **Dashboard colors** and choose a scheme or select a primary and accent ink individually. **Preview** shows the layout before you save. To change the weekly forecast, select its tile, use **Find a city**, choose a result and set °F or °C. Save the dashboard to apply it. Existing custom layouts can add a **7-day forecast** from the widget picker.

The weekly forecast shows seven dates for the selected city. Weather comes from 7Timer! and uses the chosen Fahrenheit or Celsius units. City searches use Open-Meteo and GeoNames. The app caches forecasts for three hours and can use a cached week for up to 24 hours. [7Timer!](https://www.7timer.info/), [Open-Meteo](https://open-meteo.com/), [GeoNames](https://www.geonames.org/), [geocoding licence](https://creativecommons.org/licenses/by/4.0/).

## Photos and overlays

- To alternate between the playlist and a full-screen dashboard, open **Devices**, choose **Alternate photos and dashboard**, select the dashboard, and **Save schedule**. The cycle length covers both halves. For example, 60 minutes gives each photo 30 minutes and each dashboard 30 minutes. The next photo follows playlist order. A photo can still have its own overlay.
- In **Photos**, choose **Intelligent Crop** to fill the frame around the strongest visual detail. Open the photo’s framing editor to preview a different fit, reposition the focus, or change the zoom. The original upload and previous processed image, preview, and raw frame versions stay in the app’s private Home Assistant storage.
- In **Dashboards → Photo overlays**, select your display, choose **Photo details**, and select **Apply overlay**. A lower-left caption shows the capture date in yellow, the photo’s age in red, and the location in green on a dark background. Missing fields are skipped. A photo with no capture details appears without a caption.
- For a custom overlay, create a dashboard with the widgets you want, preview it over an uploaded photo, and select the saved dashboard under **Photo overlays**. The boxes are drawn over the crop before conversion to the panel’s six colors.
- Choose **Add widget → Photo details**, then **Date taken**, **Years ago**, or **Photo location**. Add another widget to show more than one detail. Pick an uploaded photo in the preview to see its own values. Years ago uses the capture-date anniversary at the time the frame is scheduled.
- inkDaddy reads photo details from saved JPEG, PNG, and WebP originals, including earlier uploads. Location uses an embedded place name first. For coordinate-only photos, open **Photos → Photo locations → Find place names**. This one-time lookup sends GPS coordinates to Photon. Photo files, filenames and capture dates stay in Home Assistant. Check **Look up new uploads automatically** to include future uploads. Each installation starts with automatic lookup off.
- Map matches are cached locally. A close, distinguishable business may appear as “Near …”. Where the venue is ambiguous, the caption uses a street or locality. When there is no match, it keeps the rounded coordinates. Photos without location data omit that line. Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), lookup by [Photon](https://photon.komoot.io/).
- Use date, compatible Home Assistant sensor, weather, location, battery, and calendar widgets. The calendar widget shows upcoming times and “Busy” by default. Enable event titles on the widget to show them on the display and in frame previews.
- If a scheduled repeat of the same item produces the exact image already confirmed on the panel, inkDaddy keeps it on screen and saves a wireless transfer and panel refresh. The playlist advances on schedule. Press **Refresh** in **Displays** to redraw the current item or apply an edit immediately.

inkDaddy samples Home Assistant’s reported states every 30 minutes. Measurements expire after one hour. Weather providers can report less frequently, so weather stays available for up to three hours. After the first hour, the weather tile includes the provider’s report time in the Home Assistant time zone. An expired report or an unavailable provider shows **Unavailable**. Automatic weather uses the eligible provider with the newest report. With its default source, the display battery widget shows the frame’s last reported level. You can also select a Home Assistant battery sensor. The Location widget can show your configured home name or the name of a selected Home Assistant zone.

## Home Assistant playback buttons

In **Devices → Connect a display**, select **Set up playback buttons** for the linked display. Home Assistant adds Next, Previous, Play/Pause, and Refresh to the same device. The buttons use the frame’s existing Matter endpoints. The original switch entities stay enabled for automations that target them directly.

Home Assistant also creates an **Identify** diagnostic button for each endpoint that advertises a locator. Pressing one sends the standard 15-second Identify command. The v6 and v7 frame firmware start an internal timer and produce no visible LED or ePaper response. Use the playback buttons to change the frame.

## Access and data

Open the app through Home Assistant’s **Open Web UI** button. The Ingress panel requires a Home Assistant administrator and Supervisor 2026.11.0 or later. Supervisor grants the app administrator-level Home Assistant API access. inkDaddy uses it to read the local site and supported entity readings and to share a selected Matter device. Host networking lets the companion Matter controller discover and communicate with the frame over IPv6. Its management API uses a private Unix socket and a runtime-generated token.

Photos, settings, and Matter fabric credentials are stored in the app’s `/data` directory. Protect app backups as you would protect your Home Assistant backup. Matter operational credentials are created on each installation.

Photo uploads accept JPEG, PNG, WebP and BMP. Each photo is limited to 32 MiB and 50 megapixels, and a batch is limited to 50 photos and 128 MiB. inkDaddy converts supported RGB color profiles, including Display P3, to sRGB before preparing the six-color panel image. Generated previews contain the processed image and its color marker. Original uploads stay in private app storage. Dashboard YAML accepts up to 64 KiB of configuration.

## Request an integration

Open **Integrations** and enter the integration’s name in the request form. inkDaddy keeps the request locally until an owner relay is configured. With a relay configured, it sends the name and an identifier derived from the name. After you submit a request, the form shows its status and offers a retry if delivery fails. Network intermediaries can observe ordinary connection metadata.

Relay operators can configure **Settings → Apps → inkDaddy → Configuration** with `integration_relay_url` and `integration_relay_token`, then restart the app. Leave both blank to keep requests local. The repository’s `tools/INTEGRATION_REQUESTS.md` explains private relay setup, Mac mini pickup, and owner review.

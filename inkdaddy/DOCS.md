# Using inkDaddy

1. Pair the display in the Home Assistant Companion app. Scan the Matter QR code on the display.
2. Open the inkDaddy app and choose **Connect inkDaddy**. Select the display and choose **Connect display**, or enter the temporary sharing code from the display’s Matter device page.
3. Add photos and create dashboards. Pick a layout and add widgets for recent Home Assistant sensor readings, weather, local time, location, or the display’s battery. Use **Preview** to check how a dashboard will look on the chosen display.
4. Open **Display → Edit playlist** to choose what rotates on the frame. The default interval is 60 minutes. To change it, open **Devices**, enter a new interval for the frame, and select **Save schedule**. The Display tab shows the last confirmed frame and the next five scheduled slots.

When upgrading from dev.19 or earlier, keep an app backup with that version. The upgrade converts stored playback times from hours to minutes. To return to the earlier version, restore its matching app backup.

## Updates

Open **inkDaddy → Updates → Manage app in Home Assistant** to install an available app release or change automatic updates. With remote administrator access to that Home Assistant installation, you can do this away from home. Each installation controls its own updates.

The same tab lists the firmware versions Home Assistant has recorded for registered inkDaddy displays. Select a display to open its native device page. Its Matter controller handles signed firmware updates over Thread.

## Photos and overlays

- To alternate between the playlist and a full-screen dashboard, open **Devices**, choose **Alternate photos and dashboard**, select the dashboard, and **Save schedule**. The cycle length covers both halves. For example, 60 minutes gives each photo 30 minutes and each dashboard 30 minutes. The next photo follows playlist order. A photo can still have its own overlay.
- In **Manage Photos**, choose **Intelligent Crop** to fill the frame around the strongest visual detail. Open the photo’s framing editor to preview a different fit, reposition the focus, or change the zoom. The original upload and previous processed image, preview, and raw frame versions stay in the app’s private Home Assistant storage.
- In **Dashboards**, create a layout with the widgets you want over the photos, and assign it to a display as its photo overlay. Preview the layout over an uploaded photo before applying it. The boxes are drawn over the crop before conversion to the panel’s six colors.
- Choose **Add widget → Photo details**, then **Date taken**, **Years ago**, or **Photo location**. Add another widget to show more than one detail. Pick an uploaded photo in the preview to see its own values. Years ago uses the capture-date anniversary at the time the frame is scheduled.
- inkDaddy reads photo details from saved JPEG, PNG, and WebP originals, including earlier uploads. Location uses an embedded place name when available or rounded GPS coordinates. Each detail needs its own tag. Photos with missing tags still display normally. inkDaddy reads these details locally, and the selected values become visible on the frame when you assign the overlay.
- Use date, compatible Home Assistant sensor, weather, location, battery, and calendar widgets. The calendar widget shows upcoming times and “Busy” by default. Enable event titles on the widget to show them on the display and in frame previews.
- If a scheduled repeat of the same item produces the exact image already confirmed on the panel, inkDaddy keeps it on screen and saves a wireless transfer and panel refresh. The playlist advances on schedule. Press **Refresh** in **Display** to redraw the current item or apply an edit immediately.

inkDaddy samples Home Assistant’s reported states every 30 minutes. The widget picker offers eligible measurements and weather providers that reported within the past hour. Measurements and weather show **Unavailable** when their reports are stale. With its default source, the display battery widget shows the frame’s last reported level. You can also select a Home Assistant battery sensor. An older battery percentage includes its report time. The Location widget can show your configured home name or the name of a selected Home Assistant zone.

## Home Assistant playback buttons

In **Connect inkDaddy**, select **Set up HA playback buttons** for the linked display. Home Assistant adds Next, Previous, Play/Pause, and Refresh to the same device. The buttons use the frame’s existing Matter endpoints. The original switch entities stay enabled for automations that target them directly.

Home Assistant also creates an **Identify** diagnostic button for each endpoint that advertises a locator. Pressing one sends the standard 15-second Identify command. The v6 and v7 frame firmware start an internal timer and produce no visible LED or ePaper response. Use the playback buttons to change the frame.

## Access and data

Open the app through Home Assistant’s **Open Web UI** button. The Ingress panel requires a Home Assistant administrator and Supervisor 2026.11.0 or later. Supervisor grants the app administrator-level Home Assistant API access. inkDaddy uses it to read the local site and supported entity readings and to share a selected Matter device. Host networking lets the companion Matter controller discover and communicate with the frame over IPv6. Its management API uses a private Unix socket and a runtime-generated token.

Photos, settings, and Matter fabric credentials are stored in the app’s `/data` directory. Protect app backups as you would protect your Home Assistant backup. Matter operational credentials are created on each installation.

Photo uploads accept JPEG, PNG, WebP and BMP. Each photo is limited to 32 MiB and 50 megapixels, and a batch is limited to 50 photos and 128 MiB. inkDaddy converts supported RGB color profiles, including Display P3, to sRGB before preparing the six-color panel image. Generated previews contain the processed image and its color marker. Original uploads stay in private app storage. Dashboard YAML accepts up to 64 KiB of configuration.

## Request Integration

Open **Integrations → Request Integration** and enter the integration’s name. inkDaddy keeps the request locally until an owner relay is configured. With a relay configured, it sends the name and an identifier derived from the name. The delivery status shows whether the relay has accepted it for review. Network intermediaries can observe ordinary connection metadata.

Relay operators can configure **Settings → Apps → inkDaddy → Configuration** with `integration_relay_url` and `integration_relay_token`, then restart the app. Leave both blank to keep requests local. The repository’s `tools/INTEGRATION_REQUESTS.md` explains private relay setup, Mac mini pickup, and owner review.

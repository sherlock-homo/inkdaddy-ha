# Using inkDaddy

1. Pair the display in the Home Assistant Companion app. Scan the Matter QR code on the display.
2. Open the inkDaddy app and choose **Connect inkDaddy**. Select the display and choose **Connect display**, or enter the temporary sharing code from the display’s Matter device page.
3. Add photos and create dashboards. Pick a layout and add widgets for recent Home Assistant sensor readings, weather, local time, location, or the display’s battery. Use **Preview** to check how a dashboard will look on the chosen display.
4. Open **Display → Edit playlist** to choose what rotates on the frame. The display refreshes on the hour while playing. The preview follows the display’s confirmation, and the next five slots show what is prepared.

inkDaddy samples Home Assistant’s reported states every 30 minutes. The widget picker offers eligible measurements and weather providers that reported within the past hour. A stale source shows **Unavailable** when a dashboard is prepared. The clock label distinguishes the scheduled slot, a manual refresh request, and a preview. The display battery widget shows the last reported level.

## Access and data

Open the app through Home Assistant’s **Open Web UI** button. The Ingress panel requires a Home Assistant administrator and Supervisor 2026.11.0 or later. Supervisor grants the app administrator-level Home Assistant API access. inkDaddy uses it to read the local site and supported entity readings and to share a selected Matter device. Host networking lets the companion Matter controller discover and communicate with the frame over IPv6. Its management API uses a private Unix socket and a runtime-generated token.

Photos, settings, and Matter fabric credentials are stored in the app’s `/data` directory. Protect app backups as you would protect your Home Assistant backup. Matter operational credentials are created on each installation.

Photo uploads accept JPEG, PNG, WebP and BMP. Each photo is limited to 32 MiB and 50 megapixels, and a batch is limited to 50 photos and 128 MiB. inkDaddy converts supported RGB color profiles, including Display P3, to sRGB before preparing the six-color panel image. Generated previews contain the processed image and its color marker. Original uploads stay in private app storage. Dashboard YAML accepts up to 64 KiB of configuration.

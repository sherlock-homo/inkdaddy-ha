# inkDaddy for Home Assistant

inkDaddy manages photos and Home Assistant dashboards for an ePaper display over Matter and Thread. This Git repository is a Home Assistant App Store repository.

## Installation

1. [Add the inkDaddy repository in Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsherlock-homo%2Finkdaddy-ha) and confirm **Add**.
2. [Open inkDaddy in Apps](https://my.home-assistant.io/redirect/supervisor_app/?app=be6a7ccc_inkdaddy) and choose **Install**.
3. Start inkDaddy and select **Open Web UI**.

inkDaddy requires Home Assistant OS with Supervisor 2026.11.0 or later for server-side administrator-only access. The App Store builds the image from `inkdaddy/Dockerfile`. An existing installation from the Home Assistant **Local apps** folder has a separate app identity and data directory. Before moving to this repository, make an app backup and stop the local instance so it releases the Matter port. [Home Assistant repository guide](https://contributors.home-assistant.io/apps/guides/repository/) · [Supervisor access enforcement](https://github.com/home-assistant/supervisor/pull/7308)

See the [app documentation](inkdaddy/DOCS.md) for display onboarding and operation. Source is distributed under the [GNU Lesser General Public License v2.1](LICENSE).

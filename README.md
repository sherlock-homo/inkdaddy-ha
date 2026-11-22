# inkDaddy for Home Assistant

inkDaddy manages photos and Home Assistant dashboards for an ePaper display over Matter and Thread. This Git repository is a Home Assistant App Store repository.

## Installation

1. Open **Settings → Apps → Install app** in Home Assistant.
2. Open the menu, choose **Repositories**, and add the clone URL for this repository.
3. Install and start **inkDaddy**, then select **Open Web UI**.

Home Assistant OS or another deployment with Supervisor is required. The App Store builds the image from `inkdaddy/Dockerfile`. An existing installation from the Home Assistant **Local apps** folder has a separate app identity and data directory. Before moving to this repository, make an app backup and stop the local instance so it releases the Matter port. [Home Assistant repository guide](https://contributors.home-assistant.io/apps/guides/repository/)

See the [app documentation](inkdaddy/DOCS.md) for display onboarding and operation. Source is distributed under the [GNU Lesser General Public License v2.1](LICENSE).

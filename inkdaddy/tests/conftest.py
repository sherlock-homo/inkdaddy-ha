import pytest

from inkdaddy_hub.services import dashboard_forecast


@pytest.fixture(autouse=True)
def offline_public_forecasts(monkeypatch):
    # Default dashboards include Atlanta. Unit and integration suites render
    # them offline; forecast-specific tests supply their own provider fixture.
    def offline(*args, **kwargs):
        raise OSError("Forecast provider offline in test.")

    monkeypatch.setattr(dashboard_forecast, "_read_provider", offline)

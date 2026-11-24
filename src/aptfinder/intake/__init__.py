"""intake: source adapters (all through Apify) writing runs, observations and raw files. No goal filtering."""

from . import airbnb

ADAPTERS = {"airbnb": airbnb.ADAPTER}

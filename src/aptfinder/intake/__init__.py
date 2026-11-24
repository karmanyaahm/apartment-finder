"""intake: source adapters (all through Apify) writing runs, observations and raw files. No goal filtering."""

from . import airbnb, furnishedfinder, lease

ADAPTERS = {a.name: a for a in (airbnb.ADAPTER, furnishedfinder.ADAPTER, lease.ZILLOW, lease.APARTMENTS,
                                 lease.CRAIGSLIST)}

"""intake: source adapters writing runs, observations and raw files (every listing source through Apify; Google
Places is an official API called directly). No goal filtering."""

from . import airbnb, direct, furnishedfinder, lease, places

ADAPTERS = {a.name: a for a in (airbnb.ADAPTER, furnishedfinder.ADAPTER, lease.ZILLOW, lease.APARTMENTS,
                                 lease.CRAIGSLIST, direct.ADAPTER,
                                 places.ADAPTER)}

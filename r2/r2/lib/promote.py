# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of
# the Original Code is reddit Inc.
#
# All portions of the code written by reddit are Copyright (c) 2006-2013 reddit
# Inc. All Rights Reserved.
###############################################################################

from __future__ import with_statement

from collections import defaultdict, OrderedDict, namedtuple
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import itertools
import json
import math
import random
import time

from pylons import g, c
from pylons.i18n import ungettext

from r2.lib.wrapped import Wrapped
from r2.lib import (
    amqp,
    authorize,
    emailer,
    inventory,
    hooks,
)
from r2.lib.db.queries import (
    set_promote_status,
    set_underdelivered_campaigns,
    unset_underdelivered_campaigns,
)
from r2.lib.memoize import memoize
from r2.lib.organic import keep_fresh_links
from r2.lib.strings import strings
from r2.lib.template_helpers import get_domain
from r2.lib.utils import UniqueIterator, tup, to_date, weighted_lottery
from r2.models import (
    Account,
    AdWeight,
    Bid,
    DefaultSR,
    FakeAccount,
    FakeSubreddit,
    Frontpage,
    get_promote_srid,
    IDBuilder,
    Link,
    LiveAdWeights,
    MultiReddit,
    NotFound,
    PromoCampaign,
    PROMOTE_STATUS,
    PromotedLink,
    PromotionLog,
    PromotionWeights,
    Subreddit,
    traffic,
)
from r2.models.keyvalue import NamedGlobals


UPDATE_QUEUE = 'update_promos_q'
QUEUE_ALL = 'all'

PROMO_HEALTH_KEY = 'promotions_last_updated'

def _mark_promos_updated():
    NamedGlobals.set(PROMO_HEALTH_KEY, time.time())


def health_check():
    """Calculate the number of seconds since promotions were last updated"""
    return time.time() - int(NamedGlobals.get(PROMO_HEALTH_KEY, default=0))


def cost_per_mille(spend, impressions):
    """Return the cost-per-mille given ad spend and impressions."""
    if impressions:
        return 1000. * float(spend) / impressions
    else:
        return 0


def cost_per_click(spend, clicks):
    """Return the cost-per-click given ad spend and clicks."""
    if clicks:
        return float(spend) / clicks
    else:
        return 0


# attrs

def promo_traffic_url(l): # old traffic url
    domain = get_domain(cname=False, subreddit=False)
    return "http://%s/traffic/%s/" % (domain, l._id36)

def promotraffic_url(l): # new traffic url
    domain = get_domain(cname=False, subreddit=False)
    return "http://%s/promoted/traffic/headline/%s" % (domain, l._id36)

def promo_edit_url(l):
    domain = get_domain(cname=False, subreddit=False)
    return "http://%s/promoted/edit_promo/%s" % (domain, l._id36)

def pay_url(l, campaign):
    return "%spromoted/pay/%s/%s" % (g.payment_domain, l._id36, campaign._id36)

def view_live_url(l, srname):
    url = get_domain(cname=False, subreddit=False)
    if srname:
        url += '/r/%s' % srname
    return 'http://%s/?ad=%s' % (url, l._fullname)


def refund_url(link, campaign):
    return "%spromoted/refund/%s/%s" % (g.payment_domain, link._id36,
                                        campaign._id36)


# booleans

def is_promo(link):
    return (link and not link._deleted and link.promoted is not None
            and hasattr(link, "promote_status"))

def is_accepted(link):
    return (is_promo(link) and
            link.promote_status != PROMOTE_STATUS.rejected and
            link.promote_status >= PROMOTE_STATUS.accepted)

def is_unpaid(link):
    return is_promo(link) and link.promote_status == PROMOTE_STATUS.unpaid

def is_unapproved(link):
    return is_promo(link) and link.promote_status <= PROMOTE_STATUS.unseen

def is_rejected(link):
    return is_promo(link) and link.promote_status == PROMOTE_STATUS.rejected

def is_promoted(link):
    return is_promo(link) and link.promote_status == PROMOTE_STATUS.promoted

def is_live_on_sr(link, srname):
    if not is_promoted(link):
        return False
    live = scheduled_campaigns_by_link(link)
    srname = srname.lower()
    srname = srname if srname != DefaultSR.name.lower() else ''

    campaigns = PromoCampaign._byID(live, return_dict=True)
    for campaign_id in live:
        campaign = campaigns.get(campaign_id)
        if campaign and campaign.sr_name.lower() == srname:
            return True
    return False


def campaign_is_live(link, campaign_index):
    if not is_promoted(link):
        return False
    live = scheduled_campaigns_by_link(link)
    return campaign_index in live


# control functions

class RenderableCampaign():
    def __init__(self, campaign_id36, start_date, end_date, duration, bid,
                 spent, cpm, sr, priority, status):
        self.campaign_id36 = campaign_id36
        self.start_date = start_date
        self.end_date = end_date
        self.duration = duration

        if priority.cpm:
            self.bid = "%.2f" % bid
            self.spent = "%.2f" % spent
        else:
            self.bid = "N/A"
            self.spent = "N/A"

        self.cpm = cpm
        self.sr = sr
        self.priority_name = priority.name
        self.inventory_override = ("override" if priority.inventory_override
                                              else "")
        self.status = status

    @classmethod
    def create(cls, link, campaigns):
        transactions = get_transactions(link, campaigns)
        live_campaigns = scheduled_campaigns_by_link(link)
        user_is_sponsor = c.user_is_sponsor
        today = promo_datetime_now().date()
        r = []
        for camp in campaigns:
            transaction = transactions.get(camp._id)
            campaign_id36 = camp._id36
            start_date = camp.start_date.strftime("%m/%d/%Y")
            end_date = camp.end_date.strftime("%m/%d/%Y")
            ndays = camp.ndays
            duration = strings.time_label % dict(num=ndays,
                            time=ungettext("day", "days", ndays))
            bid = camp.bid
            spent = get_spent_amount(camp)
            cpm = getattr(camp, 'cpm', g.cpm_selfserve.pennies)
            sr = camp.sr_name
            live = camp._id in live_campaigns
            pending = today < to_date(camp.start_date)
            complete = (transaction and (transaction.is_charged() or
                                         transaction.is_refund()) and
                        not (live or pending))

            status = {'paid': bool(transaction),
                      'complete': complete,
                      'free': camp.is_freebie(),
                      'pay_url': pay_url(link, camp),
                      'view_live_url': view_live_url(link, sr),
                      'sponsor': user_is_sponsor,
                      'live': live,
                      'non_cpm': not camp.priority.cpm}

            if transaction and transaction.is_void():
                status['paid'] = False
                status['free'] = False

            if complete and user_is_sponsor and not transaction.is_refund():
                if spent < bid:
                    status['refund'] = True
                    status['refund_url'] = refund_url(link, camp)

            rc = cls(campaign_id36, start_date, end_date, duration, bid, spent,
                     cpm, sr, camp.priority, status)
            r.append(rc)
        return r


def get_renderable_campaigns(link, campaigns):
    campaigns, is_single = tup(campaigns, ret_is_single=True)
    r = RenderableCampaign.create(link, campaigns)
    if is_single:
        r = r[0]
    return r

def wrap_promoted(link):
    if not isinstance(link, Wrapped):
        link = Wrapped(link)
    return link

# These could be done with relationships, but that seeks overkill as
# we never query based on user and only check per-thing
def is_traffic_viewer(thing, user):
    return (c.user_is_sponsor or user._id == thing.author_id or
            user._id in getattr(thing, "promo_traffic_viewers", set()))

def add_traffic_viewer(thing, user):
    viewers = getattr(thing, "promo_traffic_viewers", set()).copy()
    if user._id not in viewers:
        viewers.add(user._id)
        thing.promo_traffic_viewers = viewers
        thing._commit()
        return True
    return False

def rm_traffic_viewer(thing, user):
    viewers = getattr(thing, "promo_traffic_viewers", set()).copy()
    if user._id in viewers:
        viewers.remove(user._id)
        thing.promo_traffic_viewers = viewers
        thing._commit()
        return True
    return False

def traffic_viewers(thing):
    return sorted(getattr(thing, "promo_traffic_viewers", set()))


def new_promotion(title, url, selftext, user, ip):
    """
    Creates a new promotion with the provided title, etc, and sets it
    status to be 'unpaid'.
    """
    sr = Subreddit._byID(get_promote_srid())
    l = Link._submit(title, url, user, sr, ip)
    l.promoted = True
    l.disable_comments = False
    PromotionLog.add(l, 'promotion created')

    if url == 'self':
        l.url = l.make_permalink_slow()
        l.is_self = True
        l.selftext = selftext

    l._commit()

    # set the status of the link, populating the query queue
    if c.user_is_sponsor or user.trusted_sponsor:
        set_promote_status(l, PROMOTE_STATUS.accepted)
    else:
        set_promote_status(l, PROMOTE_STATUS.unpaid)

    # the user has posted a promotion, so enable the promote menu unless
    # they have already opted out
    if user.pref_show_promote is not False:
        user.pref_show_promote = True
        user._commit()

    # notify of new promo
    emailer.new_promo(l)
    return l

def sponsor_wrapper(link):
    w = Wrapped(link)
    w.render_class = PromotedLink
    w.rowstyle = "promoted link"
    return w

def campaign_lock(link):
    return "edit_promo_campaign_lock_" + str(link._id)

def get_transactions(link, campaigns):
    """Return Bids for specified campaigns on the link.

    A PromoCampaign can have several bids associated with it, but the most
    recent one is recorded on the trans_id attribute. This is the one that will
    be returned.

    """

    campaigns = [c for c in campaigns if (c.trans_id != 0
                                          and c.link_id == link._id)]
    if not campaigns:
        return {}

    bids = Bid.lookup(thing_id=link._id)
    bid_dict = {(b.campaign, b.transaction): b for b in bids}
    bids_by_campaign = {c._id: bid_dict[(c._id, c.trans_id)] for c in campaigns}
    return bids_by_campaign

def new_campaign(link, dates, bid, cpm, sr, priority):
    # empty string for sr_name means target to all
    sr_name = sr.name if sr else ""
    campaign = PromoCampaign._new(link, sr_name, bid, cpm, dates[0], dates[1],
                                  priority)
    PromotionWeights.add(link, campaign._id, sr_name, dates[0], dates[1], bid)
    PromotionLog.add(link, 'campaign %s created' % campaign._id)

    if campaign.priority.cpm:
        author = Account._byID(link.author_id, data=True)
        if getattr(author, "complimentary_promos", False):
            free_campaign(link, campaign, c.user)
    else:
        # non-cpm campaigns are never charged, so we need to fire the hook now
        hooks.get_hook('promote.new_charge').call(link=link, campaign=campaign)
    return campaign


def free_campaign(link, campaign, user):
    auth_campaign(link, campaign, user, -1)

def edit_campaign(link, campaign, dates, bid, cpm, sr, priority):
    sr_name = sr.name if sr else '' # empty string means target to all
    try:
        # if the bid amount changed, cancel any pending transactions
        if campaign.bid != bid:
            void_campaign(link, campaign)

        # update the schedule
        PromotionWeights.reschedule(link, campaign._id, sr_name,
                                    dates[0], dates[1], bid)

        # update values in the db
        campaign.update(dates[0], dates[1], bid, cpm, sr_name,
                        campaign.trans_id, priority, commit=True)

        if campaign.priority.cpm:
            # record the transaction
            text = 'updated campaign %s. (bid: %0.2f)' % (campaign._id, bid)
            PromotionLog.add(link, text)

            # make it a freebie, if applicable
            author = Account._byID(link.author_id, True)
            if getattr(author, "complimentary_promos", False):
                free_campaign(link, campaign, c.user)

        hooks.get_hook('campaign.edit').call(link=link, campaign=campaign)

    except Exception, e: # record error and rethrow 
        g.log.error("Failed to update PromoCampaign %s on link %d. Error was: %r" %
                    (campaign._id, link._id, e))
        try: # wrapped in try/except so orig error won't be lost if commit fails
            text = 'update FAILED. (campaign: %s, bid: %.2f)' % (campaign._id,
                                                                 bid)
            PromotionLog.add(link, text)
        except:
            pass
        raise e


def complimentary(username, value=True):
    a = Account._by_name(username, True)
    a.complimentary_promos = value
    a._commit()

def delete_campaign(link, campaign):
    PromotionWeights.delete_unfinished(link, campaign._id)
    void_campaign(link, campaign)
    campaign.delete()
    PromotionLog.add(link, 'deleted campaign %s' % campaign._id)
    hooks.get_hook('campaign.void').call(link=link, campaign=campaign)

def void_campaign(link, campaign):
    transactions = get_transactions(link, [campaign])
    bid_record = transactions.get(campaign._id)
    if bid_record:
        a = Account._byID(link.author_id)
        authorize.void_transaction(a, bid_record.transaction, campaign._id)

def auth_campaign(link, campaign, user, pay_id):
    """
    Authorizes (but doesn't charge) a bid with authorize.net.
    Args:
    - link: promoted link
    - campaign: campaign to be authorized
    - user: Account obj of the user doing the auth (usually the currently
        logged in user)
    - pay_id: customer payment profile id to use for this transaction. (One
        user can have more than one payment profile if, for instance, they have
        more than one credit card on file.) Set pay_id to -1 for freebies.

    Returns: (True, "") if successful or (False, error_msg) if not. 
    """
    void_campaign(link, campaign)
    test = 1 if g.debug else None
    trans_id, reason = authorize.auth_transaction(campaign.bid, user, pay_id,
                                                  link, campaign._id, test=test)

    if trans_id and not reason:
        text = ('updated payment and/or bid for campaign %s: '
                'SUCCESS (trans_id: %d, amt: %0.2f)' % (campaign._id, trans_id,
                                                        campaign.bid))
        PromotionLog.add(link, text)
        if trans_id < 0:
            PromotionLog.add(link, 'FREEBIE (campaign: %s)' % campaign._id)

        if trans_id:
            new_status = max(PROMOTE_STATUS.unseen, link.promote_status)
        else:
            new_status = max(PROMOTE_STATUS.unpaid, link.promote_status)
        set_promote_status(link, new_status)
        # notify of campaign creation
        # update the query queue
        if user and (user._id == link.author_id) and trans_id > 0:
            emailer.promo_bid(link, campaign.bid, campaign.start_date)

    else:
        # something bad happend.
        text = ("updated payment and/or bid for campaign %s: FAILED ('%s')"
                % (campaign._id, reason))
        PromotionLog.add(link, text)
        trans_id = 0

    campaign.trans_id = trans_id
    campaign._commit()

    return bool(trans_id), reason



# dates are referenced to UTC, while we want promos to change at (roughly)
# midnight eastern-US.
# TODO: make this a config parameter
timezone_offset = -5 # hours
timezone_offset = timedelta(0, timezone_offset * 3600)
def promo_datetime_now(offset=None):
    now = datetime.now(g.tz) + timezone_offset
    if offset is not None:
        now += timedelta(offset)
    return now

def accept_promotion(link):
    """
    Accepting is campaign agnostic.  Accepting the ad just means that
    it is allowed to run if payment has been processed.

    If a campagn is able to run, this also requeues it.
    """
    # update the query queue
    set_promote_status(link, PROMOTE_STATUS.accepted)

    # campaigns that should be live now must be updated
    now = promo_datetime_now(0)
    promotion_weights = PromotionWeights.get_campaigns(now)
    live_campaigns = {pw.promo_idx for pw in promotion_weights
                                   if pw.thing_name == link._fullname}
    if live_campaigns:
        campaigns = PromoCampaign._byID(live_campaigns, data=True,
                                        return_dict=False)
        PromotionLog.add(link, 'has live campaigns, forcing live')
        charge_pending(0) # campaign must be charged before it will go live
        for campaign in campaigns:
            hooks.get_hook('campaign.edit').call(link=link, campaign=campaign)
        queue_changed_promo(link, "accepted")

    # campaigns that were charged and will go live in the future must be updated
    future_campaigns = [camp for camp in PromoCampaign._by_link(link._id)
                        if camp.start_date > now]
    transactions = get_transactions(link, future_campaigns)
    charged_campaigns = [camp for camp in future_campaigns
                         if (transactions.get(camp._id) and
                             transactions.get(camp._id).is_charged())]
    for campaign in charged_campaigns:
        hooks.get_hook('campaign.edit').call(link=link, campaign=campaign)

    if link._spam:
        link._spam = False
        link._commit()
    emailer.accept_promo(link)

def reject_promotion(link, reason=None):
    # update the query queue
    # Since status is updated first,
    # if make_daily_promotions happens to run
    # while we're doing work here, it will correctly exclude it
    set_promote_status(link, PROMOTE_STATUS.rejected)

    all_ads = get_live_promotions([LiveAdWeights.ALL_ADS])
    links = set(x.link for x in all_ads[LiveAdWeights.ALL_ADS])
    if link._fullname in links:
        PromotionLog.add(link, 'has live campaigns, terminating')
        queue_changed_promo(link, "rejected")

    # Send a rejection email (unless the advertiser requested the reject)
    if not c.user or c.user._id != link.author_id:
        emailer.reject_promo(link, reason=reason)

    hooks.get_hook('promotion.void').call(link=link)


def unapprove_promotion(link):
    # update the query queue
    set_promote_status(link, PROMOTE_STATUS.unseen)
    hooks.get_hook('promotion.void').call(link=link)


def accepted_campaigns(offset=0):
    now = promo_datetime_now(offset=offset)
    promo_weights = PromotionWeights.get_campaigns(now)
    all_links = Link._by_fullname(set(x.thing_name for x in promo_weights),
                                  data=True, return_dict=True)
    accepted_links = {}
    for link_fullname, link in all_links.iteritems():
        if is_accepted(link):
            accepted_links[link._id] = link

    accepted_link_ids = accepted_links.keys()
    campaign_query = PromoCampaign._query(PromoCampaign.c.link_id == accepted_link_ids,
                                          data=True)
    campaigns = dict((camp._id, camp) for camp in campaign_query)
    for pw in promo_weights:
        campaign = campaigns.get(pw.promo_idx)
        if not campaign or (not campaign.trans_id and campaign.priority.cpm):
            continue
        link = accepted_links.get(campaign.link_id)
        if not link:
            continue

        yield (link, campaign, pw.weight)


def charged_or_not_needed(campaign):
    # True if a campaign has a charged transaction or doesn't need one
    charged = authorize.is_charged_transaction(campaign.trans_id, campaign._id)
    needs_charge = campaign.priority.cpm
    return charged or not needs_charge


def get_scheduled(offset=0):
    """
    Arguments:
      offset - number of days after today you want the schedule for
    Returns:
      {'adweights':[], 'error_campaigns':[]}
      -adweights is a list of Adweight objects used in the schedule
      -error_campaigns is a list of (campaign_id, error_msg) tuples if any 
        exceptions were raised or an empty list if there were none
      Note: campaigns in error_campaigns will not be included in by_sr

    """
    adweights = []
    error_campaigns = []
    for l, campaign, weight in accepted_campaigns(offset=offset):
        try:
            if charged_or_not_needed(campaign):
                adweight = AdWeight(l._fullname, weight, campaign._fullname)
                adweights.append(adweight)
        except Exception, e: # could happen if campaign things have corrupt data
            error_campaigns.append((campaign._id, e))
    return adweights, error_campaigns

def fuzz_impressions(imps, multiple=500):
    """Return imps rounded down to nearest multiple."""
    if imps > 0:
        return int(multiple * math.floor(float(imps) / multiple))
    else:
        return 0

def charge_pending(offset=1):
    for l, camp, weight in accepted_campaigns(offset=offset):
        user = Account._byID(l.author_id)
        try:
            if charged_or_not_needed(camp):
                continue

            charge_succeeded = authorize.charge_transaction(user, camp.trans_id,
                                                            camp._id)

            if not charge_succeeded:
                continue

            hooks.get_hook('promote.new_charge').call(link=l, campaign=camp)

            if is_promoted(l):
                emailer.queue_promo(l, camp.bid, camp.trans_id)
            else:
                set_promote_status(l, PROMOTE_STATUS.pending)
                emailer.queue_promo(l, camp.bid, camp.trans_id)
            text = ('auth charge for campaign %s, trans_id: %d' %
                    (camp._id, camp.trans_id))
            PromotionLog.add(l, text)
        except:
            print "Error on %s, campaign %s" % (l, camp._id)


def scheduled_campaigns_by_link(l, date=None):
    # A promotion/campaign is scheduled/live if it's in
    # PromotionWeights.get_campaigns(now) and
    # charged_or_not_needed

    date = date or promo_datetime_now()

    if not is_accepted(l):
        return []

    scheduled = PromotionWeights.get_campaigns(date)
    campaigns = [c.promo_idx for c in scheduled if c.thing_name == l._fullname]

    # Check authorize
    accepted = []
    for campaign_id in campaigns:
        try:
            campaign = PromoCampaign._byID(campaign_id, data=True)
            if charged_or_not_needed(campaign):
                accepted.append(campaign_id)
        except NotFound:
            g.log.error("PromoCampaign %d scheduled to run on %s not found." %
                          (campaign_id, date.strftime("%Y-%m-%d")))

    return accepted

def promotion_key():
    return "current_promotions:1"

def get_live_promotions(srids):
    timer = g.stats.get_timer("promote.get_live")
    timer.start()
    weights = LiveAdWeights.get(srids)
    timer.stop()
    return weights


def srids_from_site(user, site):
    if not isinstance(site, FakeSubreddit):
        srids = {site._id}
    elif isinstance(site, MultiReddit):
        srids = set(site.sr_ids)
    elif user and not isinstance(user, FakeAccount):
        srids = set(Subreddit.user_subreddits(user, ids=True) + [""])
    else:
        srids = set(Subreddit.user_subreddits(None, ids=True) + [""])
    return srids


def srs_with_live_promos(user, site):
    srids = srids_from_site(user, site)
    weights = get_live_promotions(srids)
    srids = [srid for srid, adweights in weights.iteritems() if adweights]

    if '' in srids:
        srs = [Frontpage]
        srids.remove('')
    else:
        srs = []

    srs.extend(Subreddit._byID(srids, data=True, return_dict=False))
    return srs


def set_live_promotions(weights):
    start = time.time()
    # First, figure out which subreddits have had ads recently
    today = promo_datetime_now()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    promo_weights = PromotionWeights.get_campaigns(yesterday, tomorrow)
    subreddit_names = set(p.sr_name for p in promo_weights)
    subreddits = Subreddit._by_name(subreddit_names).values()
    # Set the default for those subreddits to no ads
    all_weights = {sr._id: [] for sr in subreddits}

    # Mix in the currently live ads
    all_weights.update(weights)
    if '' in all_weights:
        all_weights[LiveAdWeights.FRONT_PAGE] = all_weights.pop('')

    LiveAdWeights.set_all_from_weights(all_weights)
    end = time.time()
    g.log.info("promote.set_live_promotions completed in %s seconds",
               end - start)

# Gotcha: even if links are scheduled and authorized, they won't be added to 
# current promotions until they're actually charged, so make sure to call
# charge_pending() before make_daily_promotions()
def make_daily_promotions(offset=0, test=False):
    """
    Arguments:
      offset - number of days after today to get the schedule for
      test - if True, new schedule will be generated but not launched
    Raises Exception with list of campaigns that had errors if there were any
    """

    scheduled_adweights, error_campaigns = get_scheduled(offset)
    current_adweights_byid = get_live_promotions([LiveAdWeights.ALL_ADS])
    current_adweights = current_adweights_byid[LiveAdWeights.ALL_ADS]

    link_names = [aw.link for aw in itertools.chain(scheduled_adweights,
                                                    current_adweights)]
    links = Link._by_fullname(link_names, data=True)

    camp_names = [aw.campaign for aw in itertools.chain(scheduled_adweights,
                                                        current_adweights)]
    campaigns = PromoCampaign._by_fullname(camp_names, data=True)
    srs = Subreddit._by_name([camp.sr_name for camp in campaigns.itervalues()
                              if camp.sr_name])

    expired_links = ({aw.link for aw in current_adweights} -
                     {aw.link for aw in scheduled_adweights})
    for link_name in expired_links:
        link = links[link_name]
        if is_promoted(link):
            if test:
                print "unpromote", link_name
            else:
                # update the query queue
                set_promote_status(link, PROMOTE_STATUS.finished)
                emailer.finished_promo(link)

    by_srid = defaultdict(list)
    for adweight in scheduled_adweights:
        link = links[adweight.link]
        campaign = campaigns[adweight.campaign]
        if campaign.sr_name:
            sr = srs[campaign.sr_name]
            sr_id = sr._id
            sr_over_18 = sr.over_18
        else:
            sr_id = ''
            sr_over_18 = False

        if sr_over_18:
            if test:
                print "over18", link._fullname
            else:
                link.over_18 = True
                link._commit()

        if is_accepted(link) and not is_promoted(link):
            if test:
                print "promote2", link._fullname
            else:
                # update the query queue
                set_promote_status(link, PROMOTE_STATUS.promoted)
                emailer.live_promo(link)

        by_srid[sr_id].append(adweight)

    if not test:
        set_live_promotions(by_srid)
        _mark_promos_updated()
    else:
        print by_srid

    finalize_completed_campaigns(daysago=offset+1)
    hooks.get_hook('promote.make_daily_promotions').call(offset=offset)

    # after launching as many campaigns as possible, raise an exception to 
    #   report any error campaigns. (useful for triggering alerts in irc)
    if error_campaigns:
        raise Exception("Some scheduled campaigns could not be added to daily "
                        "promotions: %r" % error_campaigns)


def finalize_completed_campaigns(daysago=1):
    # PromoCampaign.end_date is utc datetime with year, month, day only
    now = datetime.now(g.tz)
    date = now - timedelta(days=daysago)
    date = date.replace(hour=0, minute=0, second=0, microsecond=0)

    q = PromoCampaign._query(PromoCampaign.c.end_date == date,
                             # exclude no transaction and freebies
                             PromoCampaign.c.trans_id > 0,
                             data=True)
    campaigns = list(q)

    if not campaigns:
        return

    # check that traffic is up to date
    earliest_campaign = min(campaigns, key=lambda camp: camp.start_date)
    start, end = get_total_run(earliest_campaign)
    missing_traffic = traffic.get_missing_traffic(start.replace(tzinfo=None),
                                                  date.replace(tzinfo=None))
    if missing_traffic:
        raise ValueError("Can't finalize campaigns finished on %s."
                         "Missing traffic from %s" % (date, missing_traffic))

    links = Link._byID([camp.link_id for camp in campaigns], data=True)
    underdelivered_campaigns = []

    for camp in campaigns:
        if hasattr(camp, 'refund_amount'):
            continue

        link = links[camp.link_id]
        billable_impressions = get_billable_impressions(camp)
        billable_amount = get_billable_amount(camp, billable_impressions)

        if billable_amount >= camp.bid:
            if hasattr(camp, 'cpm'):
                text = '%s completed with $%s billable (%s impressions @ $%s).'
                text %= (camp, billable_amount, billable_impressions, camp.cpm)
            else:
                text = '%s completed with $%s billable (pre-CPM).'
                text %= (camp, billable_amount) 
            PromotionLog.add(link, text)
            camp.refund_amount = 0.
            camp._commit()
        else:
            underdelivered_campaigns.append(camp)

        if underdelivered_campaigns:
            set_underdelivered_campaigns(underdelivered_campaigns)


def get_refund_amount(camp, billable):
    existing_refund = getattr(camp, 'refund_amount', 0.)
    charge = camp.bid - existing_refund
    refund_amount = charge - billable
    refund_amount = Decimal(str(refund_amount)).quantize(Decimal('.01'),
                                                    rounding=ROUND_UP)
    return max(float(refund_amount), 0.)


def refund_campaign(link, camp, billable_amount, billable_impressions):
    refund_amount = get_refund_amount(camp, billable_amount)
    if refund_amount <= 0:
        return

    owner = Account._byID(camp.owner_id, data=True)
    try:
        success = authorize.refund_transaction(owner, camp.trans_id,
                                               camp._id, refund_amount)
    except authorize.AuthorizeNetException as e:
        text = ('%s $%s refund failed' % (camp, refund_amount))
        PromotionLog.add(link, text)
        g.log.debug(text + ' (response: %s)' % e)
        return

    text = ('%s completed with $%s billable (%s impressions @ $%s).'
            ' %s refunded.' % (camp, billable_amount,
                               billable_impressions, camp.cpm,
                               refund_amount))
    PromotionLog.add(link, text)
    camp.refund_amount = refund_amount
    camp._commit()
    unset_underdelivered_campaigns(camp)
    emailer.refunded_promo(link)


PromoTuple = namedtuple('PromoTuple', ['link', 'weight', 'campaign'])


def get_promotion_list(srids):
    weights = get_live_promotions(srids)
    if not weights:
        return []

    promos = []
    total = 0.
    for sr_id, sr_weights in weights.iteritems():
        if sr_id not in srids:
            continue
        for link, weight, campaign in sr_weights:
            total += weight
            promos.append((link, weight, campaign))

    return [PromoTuple(link, weight / total, campaign)
            for link, weight, campaign in promos]


def lottery_promoted_links(srids, n=10):
    """Run weighted_lottery to order and choose a subset of promoted links."""
    promo_tuples = get_promotion_list(srids)
    weights = {p: p.weight for p in promo_tuples if p.weight}
    selected = []
    while weights and len(selected) < n:
        s = weighted_lottery(weights)
        del weights[s]
        selected.append(s)
    return selected


def get_total_run(thing):
    """Return the total time span this link or campaign will run.

    Starts at the start date of the earliest campaign and goes to the end date
    of the latest campaign.

    """

    if isinstance(thing, Link):
        campaigns = PromoCampaign._by_link(thing._id)
    elif isinstance(thing, PromoCampaign):
        campaigns = [thing]

    earliest = None
    latest = None
    for campaign in campaigns:
        if not charged_or_not_needed(campaign):
            continue

        if not earliest or campaign.start_date < earliest:
            earliest = campaign.start_date

        if not latest or campaign.end_date > latest:
            latest = campaign.end_date

    # a manually launched promo (e.g., sr discovery) might not have campaigns.
    if not earliest or not latest:
        latest = datetime.utcnow()
        earliest = latest - timedelta(days=30)  # last month

    # ugh this stuff is a mess. they're stored as "UTC" but actually mean UTC-5.
    earliest = earliest.replace(tzinfo=g.tz) - timezone_offset
    latest = latest.replace(tzinfo=g.tz) - timezone_offset

    return earliest, latest


def get_traffic_dates(thing):
    """Retrieve the start and end of a Promoted Link or PromoCampaign."""
    now = datetime.now(g.tz).replace(minute=0, second=0, microsecond=0)
    start, end = get_total_run(thing)
    end = min(now, end)
    return start, end


def get_billable_impressions(campaign):
    start, end = get_traffic_dates(campaign)
    if start > datetime.now(g.tz):
        return 0

    traffic_lookup = traffic.TargetedImpressionsByCodename.promotion_history
    imps = traffic_lookup(campaign._fullname, start.replace(tzinfo=None),
                          end.replace(tzinfo=None))
    billable_impressions = sum(imp for date, (imp,) in imps)
    return billable_impressions


def get_billable_amount(camp, impressions):
    if hasattr(camp, 'cpm'):
        value_delivered = impressions / 1000. * camp.cpm / 100.
        billable_amount = min(camp.bid, value_delivered)
    else:
        # pre-CPM campaigns are charged in full regardless of impressions
        billable_amount = camp.bid

    billable_amount = Decimal(str(billable_amount)).quantize(Decimal('.01'),
                                                        rounding=ROUND_DOWN)
    return float(billable_amount)


def get_spent_amount(campaign):
    if hasattr(campaign, 'refund_amount'):
        # no need to calculate spend if we've already refunded
        spent = campaign.bid - campaign.refund_amount
    elif not hasattr(campaign, 'cpm'):
        # pre-CPM campaign
        return campaign.bid
    else:
        billable_impressions = get_billable_impressions(campaign)
        spent = get_billable_amount(campaign, billable_impressions)
    return spent


def Run(offset=0, verbose=True):
    """reddit-job-update_promos: Intended to be run hourly to pull in
    scheduled changes to ads
    
    """
    if verbose:
        print "promote.py:Run() - charge_pending()"
    charge_pending(offset=offset + 1)
    charge_pending(offset=offset)
    if verbose:
        print "promote.py:Run() - amqp.add_item()"
    amqp.add_item(UPDATE_QUEUE, json.dumps(QUEUE_ALL),
                  delivery_mode=amqp.DELIVERY_TRANSIENT)
    amqp.worker.join()
    if verbose:
        print "promote.py:Run() - finished"


def run_changed(drain=False, limit=100, sleep_time=10, verbose=True):
    """reddit-consumer-update_promos: amqp consumer of update_promos_q
    
    Handles asynch accepting/rejecting of ads that are scheduled to be live
    right now
    
    """
    @g.stats.amqp_processor(UPDATE_QUEUE)
    def _run(msgs, chan):
        items = [json.loads(msg.body) for msg in msgs]
        if QUEUE_ALL in items:
            # QUEUE_ALL is just an indicator to run make_daily_promotions.
            # There's no promotion log to update in this case.
            print "Received %s QUEUE_ALL message(s)" % items.count(QUEUE_ALL)
            items = [i for i in items if i != QUEUE_ALL]
        make_daily_promotions()
        links = Link._by_fullname([i["link"] for i in items])
        for item in items:
            PromotionLog.add(links[item['link']],
                             "Finished remaking current promotions (this link "
                             "was: %(message)s" % item)
    amqp.handle_items(UPDATE_QUEUE, _run, limit=limit, drain=drain,
                      sleep_time=sleep_time, verbose=verbose)


def queue_changed_promo(link, message):
    msg = {"link": link._fullname, "message": message}
    amqp.add_item(UPDATE_QUEUE, json.dumps(msg),
                  delivery_mode=amqp.DELIVERY_TRANSIENT)

import os
import base64
import tweepy
import logging
import asyncio
import subprocess
from datetime import datetime
from aiohttp import web
from aiohttp_session import setup, get_session, new_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import jinja2
import aiohttp_jinja2
from aiopg.sa import create_engine
import stripe

from sqlalchemy import or_

from common import twitter_api
from db import User, Tip, Job


async def _logged_in_user(session):
    """
    Return the currently logged in user
    """
    if "twitter_id" in session:
        # Get the user
        user = await User.query.where(
            User.twitter_id == session["twitter_id"]
        ).gino.first()

        # Get the twitter API for the user, and make sure it works
        try:
            api = await twitter_api(user)
            api.me()
            return user
        except:
            return None
    return None


async def _api_validate(expected_fields, json_data):
    for field in expected_fields:
        if field not in json_data:
            raise web.HTTPBadRequest(text=f"Missing field: {field}")

        invalid_type = False
        if type(expected_fields[field]) == list:
            if type(json_data[field]) not in expected_fields[field]:
                invald_type = True
        else:
            if type(json_data[field]) != expected_fields[field]:
                invalid_type = True
        if invalid_type:
            raise web.HTTPBadRequest(
                text=f"Invalid type: {field} should be {expected_fields[field]}, not {type(json_data[field])}"
            )


def authentication_required_401(func):
    async def wrapper(request):
        session = await get_session(request)
        user = await _logged_in_user(session)
        if not user:
            raise web.HTTPUnauthorized(text="Authentication required")
        return await func(request)

    return wrapper


def authentication_required_302(func):
    async def wrapper(request):
        session = await get_session(request)
        user = await _logged_in_user(session)
        if not user:
            raise web.HTTPFound(location="/")
        return await func(request)

    return wrapper


async def auth_login(request):
    session = await new_session(request)
    user = await _logged_in_user(session)
    if user:
        # If we're already logged in, redirect
        auth = tweepy.OAuthHandler(
            os.environ.get("TWITTER_CONSUMER_TOKEN"),
            os.environ.get("TWITTER_CONSUMER_KEY"),
        )
        auth.set_access_token(
            user.twitter_access_token, user.twitter_access_token_secret
        )
        api = tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True)

        # Validate user
        twitter_user = api.me()
        if session["twitter_id"] == twitter_user.id:
            raise web.HTTPFound("/app")

    # Otherwise, authorize with Twitter
    try:
        auth = tweepy.OAuthHandler(
            os.environ.get("TWITTER_CONSUMER_TOKEN"),
            os.environ.get("TWITTER_CONSUMER_KEY"),
        )
        redirect_url = auth.get_authorization_url()
        raise web.HTTPFound(location=redirect_url)
    except tweepy.TweepError:
        raise web.HTTPUnauthorized(
            text="Error, failed to get request token from Twitter"
        )


async def auth_logout(request):
    session = await get_session(request)
    del session["twitter_id"]
    raise web.HTTPFound(location="/")


async def auth_twitter_callback(request):
    params = request.rel_url.query
    if "denied" in params:
        raise web.HTTPFound(location="/")

    if "oauth_token" not in params or "oauth_verifier" not in params:
        raise web.HTTPUnauthorized(
            text="Error, oauth_token and oauth_verifier are required"
        )

    oauth_token = params["oauth_token"]
    verifier = params["oauth_verifier"]

    # Authenticate with twitter
    session = await get_session(request)
    auth = tweepy.OAuthHandler(
        os.environ.get("TWITTER_CONSUMER_TOKEN"),
        os.environ.get("TWITTER_CONSUMER_KEY"),
    )
    auth.request_token = {
        "oauth_token": oauth_token,
        "oauth_token_secret": verifier,
    }

    try:
        auth.get_access_token(verifier)
    except tweepy.TweepError:
        raise web.HTTPUnauthorized(text="Error, failed to get access token")

    try:
        api = tweepy.API(auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True)
        twitter_user = api.me()
    except tweepy.TweepError:
        raise web.HTTPUnauthorized(text="Error, error using Twitter API")

    # Save values in the session
    session["twitter_id"] = twitter_user.id

    # Does this user already exist?
    user = await User.query.where(User.twitter_id == twitter_user.id).gino.first()
    if user is None:
        # Create a new user
        user = await User.create(
            twitter_id=twitter_user.id,
            twitter_screen_name=twitter_user.screen_name,
            twitter_access_token=auth.access_token,
            twitter_access_token_secret=auth.access_token_secret,
        )

        # Create a new fetch job
        await Job.create(
            user_id=user.id,
            job_type="fetch",
            status="pending",
            scheduled_timestamp=datetime.now(),
        )

    # Redirect to app
    raise web.HTTPFound(location="/app")


@authentication_required_401
async def api_get_user(request):
    """
    Respond with information about the logged in user
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    api = await twitter_api(user)
    twitter_user = api.me()

    return web.json_response(
        {
            "user_screen_name": user.twitter_screen_name,
            "user_profile_url": twitter_user.profile_image_url_https,
            "last_fetch": user.last_fetch,
        }
    )


@authentication_required_401
async def api_get_settings(request):
    """
    Respond with the logged in user's settings
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    return web.json_response(
        {
            "delete_tweets": user.delete_tweets,
            "tweets_days_threshold": user.tweets_days_threshold,
            "tweets_retweet_threshold": user.tweets_retweet_threshold,
            "tweets_like_threshold": user.tweets_like_threshold,
            "tweets_threads_threshold": user.tweets_threads_threshold,
            "retweets_likes": user.retweets_likes,
            "retweets_likes_delete_retweets": user.retweets_likes_delete_retweets,
            "retweets_likes_retweets_threshold": user.retweets_likes_retweets_threshold,
            "retweets_likes_delete_likes": user.retweets_likes_delete_likes,
            "retweets_likes_likes_threshold": user.retweets_likes_likes_threshold,
        }
    )


@authentication_required_401
async def api_post_settings(request):
    """
    Update the settings for the currently-logged in user
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    data = await request.json()

    # Validate
    await _api_validate(
        {
            "delete_tweets": bool,
            "tweets_days_threshold": int,
            "tweets_retweet_threshold": int,
            "tweets_like_threshold": int,
            "tweets_threads_threshold": bool,
            "retweets_likes": bool,
            "retweets_likes_delete_retweets": bool,
            "retweets_likes_retweets_threshold": int,
            "retweets_likes_delete_likes": bool,
            "retweets_likes_likes_threshold": int,
        },
        data,
    )

    # Update settings in the database
    await user.update(
        delete_tweets=data["delete_tweets"],
        tweets_days_threshold=data["tweets_days_threshold"],
        tweets_retweet_threshold=data["tweets_retweet_threshold"],
        tweets_like_threshold=data["tweets_like_threshold"],
        tweets_threads_threshold=data["tweets_threads_threshold"],
        retweets_likes=data["retweets_likes"],
        retweets_likes_delete_retweets=data["retweets_likes_delete_retweets"],
        retweets_likes_retweets_threshold=data["retweets_likes_retweets_threshold"],
        retweets_likes_delete_likes=data["retweets_likes_delete_likes"],
        retweets_likes_likes_threshold=data["retweets_likes_likes_threshold"],
    ).apply()

    return web.json_response(True)


@authentication_required_401
async def api_get_tip(request):
    """
    Respond with all information necessary for Stripe tips
    """
    return web.json_response(
        {"stripe_publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY")}
    )


@authentication_required_302
async def api_post_tip(request):
    """
    Charge the credit card
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    data = await request.json()

    # Validate
    await _api_validate(
        {"token": str, "amount": str, "other_amount": [str, float],}, data,
    )
    if (
        data["amount"] != "100"
        and data["amount"] != "200"
        and data["amount"] != "500"
        and data["amount"] != "1337"
        and data["amount"] != "2000"
        and data["amount"] != "other"
    ):
        return web.json_response({"error": True, "error_message": "Invalid amount"})
    if data["amount"] == "other":
        if float(data["other_amount"]) < 0:
            return web.json_response(
                {
                    "error": True,
                    "error_message": "Mess with the best, die like the rest",
                }
            )
        elif float(data["other_amount"]) < 1:
            return web.json_response(
                {"error": True, "error_message": "You must tip at least $1"}
            )

    # How much is being tipped?
    if data["amount"] == "other":
        amount = int(float(data["other_amount"]) * 100)
    else:
        amount = int(data["amount"])

    # Charge the card
    try:
        charge = stripe.Charge.create(
            amount=amount, currency="usd", description="Tip", source=data["token"],
        )

        # Add tip to the database
        timestamp = datetime.utcfromtimestamp(charge.created)
        await Tip.create(
            user_id=user.id,
            charge_id=charge.id,
            receipt_url=charge.receipt_url,
            paid=charge.paid,
            refunded=charge.refunded,
            amount=amount,
            timestamp=timestamp,
        )
        return web.json_response({"error": False})

    except stripe.error.CardError as e:
        return web.json_response(
            {"error": True, "error_message": f"Card error: {e.error.message}"}
        )
    except stripe.error.RateLimitError as e:
        return web.json_response(
            {"error": True, "error_message": f"Rate limit error: {e.error.message}"}
        )
    except stripe.error.InvalidRequestError as e:
        return web.json_response(
            {
                "error": True,
                "error_message": f"Invalid request error: {e.error.message}",
            }
        )
    except stripe.error.AuthenticationError as e:
        return web.json_response(
            {"error": True, "error_message": f"Authentication error: {e.error.message}"}
        )
    except stripe.error.APIConnectionError as e:
        return web.json_response(
            {
                "error": True,
                "error_message": f"Network communication with Stripe error: {e.error.message}",
            }
        )
    except stripe.error.StripeError as e:
        return web.json_response(
            {"error": True, "error_message": f"Unknown Stripe error: {e.error.message}"}
        )
    except Exception as e:
        return web.json_response(
            {"error": True, "error_message": f"Something went wrong, sorry: {e}"}
        )


@authentication_required_401
async def api_get_tip_recent(request):
    """
    Respond with the receipt_url for the most recent tip from this user
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    tip = (
        await Tip.query.where(User.id == user.id)
        .where(Tip.paid == True)
        .where(Tip.refunded == False)
        .order_by(Tip.timestamp.desc())
        .gino.first()
    )

    if tip:
        receipt_url = tip.receipt_url
    else:
        receipt_url = None

    return web.json_response({"receipt_url": receipt_url})


@authentication_required_401
async def api_get_tip_history(request):
    """
    Respond with a list of all tips the user has given
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    tips = (
        await Tip.query.where(User.id == user.id)
        .where(Tip.paid == True)
        .where(Tip.refunded == False)
        .order_by(Tip.timestamp.desc())
        .gino.all()
    )

    return web.json_response(
        [
            {
                "timestamp": tip.timestamp.timestamp(),
                "amount": tip.amount,
                "receipt_url": tip.receipt_url,
            }
            for tip in tips
        ]
    )


@authentication_required_401
async def api_get_job(request):
    """
    Respond with the current user's list of active and pending jobs
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    pending_jobs = (
        await Job.query.where(User.id == user.id)
        .where(Job.status == "pending")
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    active_jobs = (
        await Job.query.where(User.id == user.id)
        .where(Job.status == "active")
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    def to_client(jobs):
        jobs_json = []
        for job in jobs:
            if job.scheduled_timestamp:
                scheduled_timestamp = job.scheduled_timestamp.timestamp()
            else:
                scheduled_timestamp = None
            if job.started_timestamp:
                started_timestamp = job.started_timestamp.timestamp()
            else:
                started_timestamp = None

            jobs_json.append(
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "progress": job.progress,
                    "status": job.status,
                    "scheduled_timestamp": scheduled_timestamp,
                    "started_timestamp": started_timestamp,
                }
            )
            return jobs_json

    return web.json_response(
        {
            "pending_jobs": to_client(pending_jobs),
            "active_jobs": to_client(active_jobs),
            "paused": user.paused,
        }
    )


@authentication_required_401
async def api_post_job(request):
    """
    Either start or pause semiphemeral.

    If action is start, the user paused, and there are no pending or active jobs, unpause and create a delete job.
    If action is pause and the user is not paused, cancel any active or pending jobs and pause.
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    data = await request.json()

    # Validate
    await _api_validate({"action": str}, data)
    if data["action"] != "start" and data["action"] != "pause":
        raise web.HTTPBadRequest(text="action must be 'start' or 'pause")

    # Get pending and active jobs
    pending_jobs = (
        await Job.query.where(Job.user_id == user.id)
        .where(Job.status == "pending")
        .gino.all()
    )
    active_jobs = (
        await Job.query.where(Job.user_id == user.id)
        .where(Job.status == "active")
        .gino.all()
    )
    jobs = pending_jobs + active_jobs

    if data["action"] == "start":
        if not user.paused:
            raise web.HTTPBadRequest(
                text="Cannot 'start' unless semiphemeral is paused"
            )
        if len(jobs) > 0:
            raise web.HTTPBadRequest(
                text="Cannot 'start' when there are pending or active jobs"
            )

            # Create a new delete job
            await Job.create(
                user_id=user.id,
                job_type="delete",
                status="pending",
                scheduled_timestamp=datetime.now(),
            )

            # Unpause
            await user.update(paused=False).apply()

    elif data["action"] == "pause":
        if user.paused:
            raise web.HTTPBadRequest(
                text="Cannot 'pause' when semiphemeral is already paused"
            )

        # Cancel jobs
        for job in jobs:
            await job.update(status="canceled").apply()

        # Pause
        await user.update(paused=True).apply()

    return web.json_response(True)


@aiohttp_jinja2.template("index.jinja2")
async def index(request):
    session = await get_session(request)
    user = await _logged_in_user(session)
    logged_in = user is not None
    return {"logged_in": logged_in}


@aiohttp_jinja2.template("app.jinja2")
@authentication_required_302
async def app_main(request):
    return {"deploy_environment": os.environ.get("DEPLOY_ENVIRONMENT")}


async def start_web_server():
    # Create the web app
    app = web.Application()
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader("templates"))
    logging.basicConfig(filename="/var/web/web.log", level=logging.DEBUG)

    # Secret_key must be 32 url-safe base64-encoded bytes
    fernet_key = os.environ.get("COOKIE_FERNET_KEY")
    secret_key = base64.urlsafe_b64decode(fernet_key)
    setup(app, EncryptedCookieStorage(secret_key))

    # Define routes
    app.add_routes(
        [
            # Static files
            web.static("/static", "static"),
            # Authentication
            web.get("/auth/login", auth_login),
            web.get("/auth/logout", auth_logout),
            web.get("/auth/twitter_callback", auth_twitter_callback),
            # API
            web.get("/api/user", api_get_user),
            web.get("/api/settings", api_get_settings),
            web.post("/api/settings", api_post_settings),
            web.get("/api/tip", api_get_tip),
            web.post("/api/tip", api_post_tip),
            web.get("/api/tip/recent", api_get_tip_recent),
            web.get("/api/tip/history", api_get_tip_history),
            web.get("/api/job", api_get_job),
            web.post("/api/job", api_post_job),
            # Web
            web.get("/", index),
            web.get("/app", app_main),
        ]
    )

    loop = asyncio.get_event_loop()
    server = await loop.create_server(app.make_handler(), port=8080)
    await server.serve_forever()

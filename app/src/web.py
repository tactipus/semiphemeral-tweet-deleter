import os
import base64
import tweepy
import logging
import asyncio
import functools
import subprocess
import csv
import json
from datetime import datetime, timedelta
from aiohttp import web
from aiohttp_session import setup, get_session, new_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import jinja2
import aiohttp_jinja2
from aiopg.sa import create_engine
import stripe

from sqlalchemy import or_

from common import (
    twitter_api,
    twitter_dms_api,
    twitter_api_call,
    tweets_to_delete,
    send_admin_dm,
)
from db import (
    User,
    Tip,
    Nag,
    Job,
    DirectMessageJob,
    BlockJob,
    UnblockJob,
    Tweet,
    Thread,
    Fascist,
)


async def _logged_in_user(session):
    """
    Return the currently logged in user
    """
    if "twitter_id" in session:
        # Get the user
        user = await User.query.where(
            User.twitter_id == session["twitter_id"]
        ).gino.first()
        if not user:
            del session["twitter_id"]
            return None

        # Are we the administrator impersonating another user?
        if (
            user.twitter_screen_name == os.environ.get("ADMIN_USERNAME")
            and "impersonating_twitter_id" in session
        ):
            print(
                f"Admin impersonating user with id {session['impersonating_twitter_id']}"
            )
            impersonating_user = await User.query.where(
                User.twitter_id == session["impersonating_twitter_id"]
            ).gino.first()
            return impersonating_user

        return user

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


async def _api_validate_dms_authenticated(user):
    if (
        user.twitter_dms_access_token != ""
        and user.twitter_dms_access_token_secret != ""
    ):
        # Check if user is authenticated with DMs twitter app
        try:
            dms_api = await twitter_dms_api(user)
            twitter_user = await twitter_api_call(dms_api, "me")
            return True
        except:
            pass

    return False


def authentication_required_401(func):
    async def wrapper(request):
        session = await get_session(request)
        if "twitter_id" not in session:
            raise web.HTTPUnauthorized(text="Authentication required")
        user = await User.query.where(
            User.twitter_id == session["twitter_id"]
        ).gino.first()
        if not user:
            raise web.HTTPUnauthorized(text="Authentication required")
        return await func(request)

    return wrapper


def authentication_required_302(func):
    async def wrapper(request):
        session = await get_session(request)
        if "twitter_id" not in session:
            raise web.HTTPFound(location="/")
        user = await User.query.where(
            User.twitter_id == session["twitter_id"]
        ).gino.first()
        if not user:
            raise web.HTTPFound(location="/")
        return await func(request)

    return wrapper


def admin_required(func):
    async def wrapper(request):
        session = await get_session(request)
        user = await User.query.where(
            User.twitter_id == session["twitter_id"]
        ).gino.first()
        if not user or user.twitter_screen_name != os.environ.get("ADMIN_USERNAME"):
            raise web.HTTPNotFound()
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
        twitter_user = await twitter_api_call(api, "me")
        if session["twitter_id"] == str(twitter_user.id):
            raise web.HTTPFound("/dashboard")

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
    if "impersonating_twitter_id" in session:
        del session["impersonating_twitter_id"]
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
        twitter_user = await twitter_api_call(api, "me")
    except tweepy.TweepError as e:
        raise web.HTTPUnauthorized(text=f"Error, error using Twitter API: {e}")

    # Save values in the session
    session["twitter_id"] = str(twitter_user.id)

    # Does this user already exist?
    user = await User.query.where(User.twitter_id == str(twitter_user.id)).gino.first()
    if user is None:
        # Create a new user
        user = await User.create(
            twitter_id=str(twitter_user.id),
            twitter_screen_name=twitter_user.screen_name,
            twitter_access_token=auth.access_token,
            twitter_access_token_secret=auth.access_token_secret,
            paused=True,
            blocked=False,
        )

        # Create a new fetch job
        await Job.create(
            user_id=user.id,
            job_type="fetch",
            status="pending",
            scheduled_timestamp=datetime.now(),
        )
    else:
        # Make sure to update the user's twitter access token and secret
        await user.update(
            twitter_access_token=auth.access_token,
            twitter_access_token_secret=auth.access_token_secret,
        ).apply()

    # Redirect to app
    raise web.HTTPFound(location="/dashboard")


async def auth_twitter_dms_callback(request):
    params = request.rel_url.query
    # If denied, I guess just redirect to homepage?
    if "denied" in params:
        raise web.HTTPFound(location="/")

    if "oauth_token" not in params or "oauth_verifier" not in params:
        raise web.HTTPUnauthorized(
            text="Error, oauth_token and oauth_verifier are required"
        )

    oauth_token = params["oauth_token"]
    verifier = params["oauth_verifier"]

    # Authenticate with twitter (DMs)
    session = await get_session(request)
    auth = tweepy.OAuthHandler(
        os.environ.get("TWITTER_DM_CONSUMER_TOKEN"),
        os.environ.get("TWITTER_DM_CONSUMER_KEY"),
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
        twitter_user = await twitter_api_call(api, "me")
    except tweepy.TweepError as e:
        raise web.HTTPUnauthorized(text=f"Error, error using Twitter API: {e}")

    # Does this user already exist?
    user = await User.query.where(User.twitter_id == str(twitter_user.id)).gino.first()
    if user is None:
        # Uh, that's weird, there really should already be a user... so just ignore in that case
        pass
    else:
        # Update the user's DM twitter access token and secret
        await user.update(
            twitter_dms_access_token=auth.access_token,
            twitter_dms_access_token_secret=auth.access_token_secret,
        ).apply()

    # Redirect to settings page again
    raise web.HTTPFound(location="/settings")


async def stripe_callback(request):
    data = await request.json()

    # Refund a charge
    if data["type"] == "charge.refunded":
        charge_id = data["data"]["object"]["id"]
        tip = await Tip.query.where(Tip.charge_id == charge_id).gino.first()
        if tip:
            await tip.update(refunded=True).apply()

    return web.HTTPOk()


@authentication_required_401
async def api_get_user(request):
    """
    Respond with information about the logged in user
    """
    session = await get_session(request)

    # Get the actual logged in user
    user = await User.query.where(User.twitter_id == session["twitter_id"]).gino.first()

    # Are we the administrator impersonating another user?
    if (
        user.twitter_screen_name == os.environ.get("ADMIN_USERNAME")
        and "impersonating_twitter_id" in session
    ):
        # Load the API using the admin user
        api = await twitter_api(user)
        # Load the impersonated user
        user = await _logged_in_user(session)
        twitter_user = await twitter_api_call(
            api, "get_user", screen_name=user.twitter_screen_name
        )
    else:
        # Just a normal user
        api = await twitter_api(user)
        twitter_user = await twitter_api_call(api, "me")

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

    has_fetched = user.since_id != None
    is_dm_app_authenticated = await _api_validate_dms_authenticated(user)

    return web.json_response(
        {
            "has_fetched": has_fetched,
            "delete_tweets": user.delete_tweets,
            "tweets_days_threshold": user.tweets_days_threshold,
            "tweets_enable_retweet_threshold": user.tweets_enable_retweet_threshold,
            "tweets_retweet_threshold": user.tweets_retweet_threshold,
            "tweets_enable_like_threshold": user.tweets_enable_like_threshold,
            "tweets_like_threshold": user.tweets_like_threshold,
            "tweets_threads_threshold": user.tweets_threads_threshold,
            "retweets_likes": user.retweets_likes,
            "retweets_likes_delete_retweets": user.retweets_likes_delete_retweets,
            "retweets_likes_retweets_threshold": user.retweets_likes_retweets_threshold,
            "retweets_likes_delete_likes": user.retweets_likes_delete_likes,
            "retweets_likes_likes_threshold": user.retweets_likes_likes_threshold,
            "direct_messages": user.direct_messages,
            "direct_messages_threshold": user.direct_messages_threshold,
            "is_dm_app_authenticated": is_dm_app_authenticated,
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
    await _api_validate({"action": str}, data)
    if data["action"] != "save" and data["action"] != "authenticate_dms":
        raise web.HTTPBadRequest(text="action must be 'save' or 'authenticate_dms'")

    if data["action"] == "save":
        # Validate some more
        await _api_validate(
            {
                "action": str,
                "delete_tweets": bool,
                "tweets_days_threshold": int,
                "tweets_enable_retweet_threshold": bool,
                "tweets_retweet_threshold": int,
                "tweets_enable_like_threshold": bool,
                "tweets_like_threshold": int,
                "tweets_threads_threshold": bool,
                "retweets_likes": bool,
                "retweets_likes_delete_retweets": bool,
                "retweets_likes_retweets_threshold": int,
                "retweets_likes_delete_likes": bool,
                "retweets_likes_likes_threshold": int,
                "direct_messages": bool,
                "direct_messages_threshold": int,
                "download_all_tweets": bool,
            },
            data,
        )

        # Update settings in the database
        direct_messages_threshold = int(data["direct_messages_threshold"])
        if direct_messages_threshold > 29:
            direct_messages_threshold = 29

        await user.update(
            delete_tweets=data["delete_tweets"],
            tweets_days_threshold=data["tweets_days_threshold"],
            tweets_enable_retweet_threshold=data["tweets_enable_retweet_threshold"],
            tweets_retweet_threshold=data["tweets_retweet_threshold"],
            tweets_enable_like_threshold=data["tweets_enable_like_threshold"],
            tweets_like_threshold=data["tweets_like_threshold"],
            tweets_threads_threshold=data["tweets_threads_threshold"],
            retweets_likes=data["retweets_likes"],
            retweets_likes_delete_retweets=data["retweets_likes_delete_retweets"],
            retweets_likes_retweets_threshold=data["retweets_likes_retweets_threshold"],
            retweets_likes_delete_likes=data["retweets_likes_delete_likes"],
            retweets_likes_likes_threshold=data["retweets_likes_likes_threshold"],
            direct_messages=data["direct_messages"],
            direct_messages_threshold=direct_messages_threshold,
        ).apply()

        # Does the user want to force downloading all tweets next time?
        if data["download_all_tweets"]:
            await user.update(since_id=None).apply()

        return web.json_response(True)

    if data["action"] == "authenticate_dms":
        # Authorize with Twitter
        try:
            auth = tweepy.OAuthHandler(
                os.environ.get("TWITTER_DM_CONSUMER_TOKEN"),
                os.environ.get("TWITTER_DM_CONSUMER_KEY"),
            )
            redirect_url = auth.get_authorization_url()
            return web.json_response({"error": False, "redirect_url": redirect_url})
        except tweepy.TweepError as e:
            return web.json_response({"error": True, "error_message": str(e)})


@authentication_required_401
async def api_post_settings_delete_account(request):
    """
    Delete the account and all data associated with the user, and log out
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    # Log the user out
    session = await get_session(request)
    del session["twitter_id"]

    # Delete everything
    # await Tip.delete.where(Tip.user_id == user.id).gino.status()
    await Nag.delete.where(Nag.user_id == user.id).gino.status()
    await Job.delete.where(Job.user_id == user.id).gino.status()
    await Thread.delete.where(Thread.user_id == user.id).gino.status()
    await Tweet.delete.where(Tweet.user_id == user.id).gino.status()
    await user.delete()

    return web.json_response(True)


@authentication_required_302
async def api_get_export_download(request):
    """
    Download CSV export of tweets
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    # Create the CSV
    os.makedirs(
        os.path.join("/tmp", "export", str(user.twitter_screen_name)), exist_ok=True
    )
    csv_filename = os.path.join(
        "/tmp", "export", str(user.twitter_screen_name), "export.csv"
    )
    with open(csv_filename, "w") as f:
        fieldnames = [
            "Date",  # created_at
            "Username",  # twitter_user_screen_name
            "Tweet ID",  # status_id
            "Text",  # text
            "Replying to Username",  # in_reply_to_screen_name
            "Replying to Tweet ID",  # in_reply_to_status_id
            "Retweets",  # retweet_count
            "Likes",  # favorite_count
            "Retweeted",  # is_retweet
            "Liked",  # favorited
            "URL",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, dialect="unix")
        writer.writeheader()

        tweets = (
            await Tweet.query.where(Tweet.user_id == user.id)
            .where(Tweet.twitter_user_id == user.twitter_id)
            .where(Tweet.is_deleted == False)
            .where(Tweet.is_unliked == False)
            .order_by(Tweet.created_at.desc())
            .gino.all()
        )
        for tweet in tweets:
            url = f"https://twitter.com/{user.twitter_screen_name}/status/{tweet.status_id}"

            # Write the row
            writer.writerow(
                {
                    "Date": tweet.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "Username": tweet.twitter_user_screen_name,
                    "Tweet ID": str(tweet.status_id),
                    "Text": tweet.text,
                    "Replying to Username": tweet.in_reply_to_screen_name,
                    "Replying to Tweet ID": str(tweet.in_reply_to_status_id),
                    "Retweets": str(tweet.retweet_count),
                    "Likes": str(tweet.favorite_count),
                    "Retweeted": str(tweet.is_retweet),
                    "Liked": str(tweet.favorited),
                    "URL": url,
                }
            )

    download_filename = f"semiphemeral-export-{user.twitter_screen_name}-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return web.FileResponse(
        csv_filename,
        headers={"Content-Disposition": f'attachment; filename="{download_filename}"'},
    )


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
        {
            "token": str,
            "amount": str,
            "other_amount": [str, float],
        },
        data,
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
        loop = asyncio.get_running_loop()
        charge = await loop.run_in_executor(
            None,
            functools.partial(
                stripe.Charge.create,
                amount=amount,
                currency="usd",
                description="Tip",
                source=data["token"],
            ),
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

        # Send a DM to the admin
        amount_dollars = amount / 100
        await send_admin_dm(
            f"@{user.twitter_screen_name} send you a ${amount_dollars} tip!"
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
        await Tip.query.where(Tip.id == user.id)
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
        await Tip.query.where(Tip.user_id == user.id)
        .order_by(Tip.timestamp.desc())
        .gino.all()
    )

    return web.json_response(
        [
            {
                "timestamp": tip.timestamp.timestamp(),
                "amount": tip.amount,
                "paid": tip.paid,
                "refunded": tip.refunded,
                "receipt_url": tip.receipt_url,
            }
            for tip in tips
        ]
    )


@authentication_required_401
async def api_get_dashboard(request):
    """
    Respond with the current user's list of active and pending jobs
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    pending_jobs = (
        await Job.query.where(Job.user_id == user.id)
        .where(Job.status == "pending")
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    active_jobs = (
        await Job.query.where(Job.user_id == user.id)
        .where(Job.status == "active")
        .order_by(Job.started_timestamp)
        .gino.all()
    )

    finished_jobs = (
        await Job.query.where(Job.user_id == user.id)
        .where(Job.status == "finished")
        .order_by(Job.finished_timestamp.desc())
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
            if job.finished_timestamp:
                finished_timestamp = job.finished_timestamp.timestamp()
            else:
                finished_timestamp = None

            jobs_json.append(
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "progress": job.progress,
                    "status": job.status,
                    "scheduled_timestamp": scheduled_timestamp,
                    "started_timestamp": started_timestamp,
                    "finished_timestamp": finished_timestamp,
                }
            )
        return jobs_json

    fascist_tweets = []
    if user.blocked:
        # Get fascist tweets that this user has liked
        fascist_tweets = (
            await Tweet.query.where(Tweet.user_id == user.id)
            .where(Tweet.favorited == True)
            .where(Tweet.is_fascist == True)
            .order_by(Tweet.created_at.desc())
            .gino.all()
        )
        fascist_tweets = [
            {
                "status_id": tweet.status_id,
                "permalink": f"https://twitter.com/{tweet.twitter_user_screen_name}/status/{tweet.status_id}",
            }
            for tweet in fascist_tweets
        ]
        # Don't return the list if there's more than 5
        if len(fascist_tweets) > 5:
            fascist_tweets = []

    return web.json_response(
        {
            "pending_jobs": to_client(pending_jobs),
            "active_jobs": to_client(active_jobs),
            "finished_jobs": to_client(finished_jobs),
            "setting_paused": user.paused,
            "setting_blocked": user.blocked,
            "setting_delete_tweets": user.delete_tweets,
            "setting_retweets_likes": user.retweets_likes,
            "setting_direct_messages": user.direct_messages,
            "fascist_tweets": fascist_tweets,
        }
    )


@authentication_required_401
async def api_post_dashboard(request):
    """
    Start or pause semiphemeral, or fetch.

    If action is start, the user paused, and there are no pending or active jobs:
      unpause and create a delete job
    If action is pause and the user is not paused:
      cancel any active or pending jobs and pause
    If action is fetch, the user is paused, and there are no pending or active jobs:
      create a fetch job
    If action is unblock and the user is blocked and hasn't liked too many fascist tweets:
      create an unblock job
    If action is reactivate and the user is blocked:
      see if the user is still blocked, and if not set blocked=False and create a fetch job
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    data = await request.json()

    # Validate
    await _api_validate({"action": str}, data)
    if (
        data["action"] != "start"
        and data["action"] != "pause"
        and data["action"] != "fetch"
        and data["action"] != "unblock"
        and data["action"] != "reactivate"
    ):
        raise web.HTTPBadRequest(
            text="action must be 'start', 'pause', 'fetch', or 'reactivate'"
        )

    if data["action"] == "unblock":
        if not user.blocked:
            raise web.HTTPBadRequest(text="Can only 'unblock' if the user is blocked")

        # Are we still blocked?
        api = await twitter_api(user)
        friendship = (
            await twitter_api_call(
                api,
                "show_friendship",
                source_id=user.twitter_id,
                target_screen_name="semiphemeral",
            )
        )[0]

        if friendship.blocked_by:
            # Still blocked by semiphemeral. Should we unblock?

            # Count fascist tweets
            fascist_tweets = (
                await Tweet.query.where(Tweet.user_id == user.id)
                .where(Tweet.favorited == True)
                .where(Tweet.is_fascist == True)
                .order_by(Tweet.created_at.desc())
                .gino.all()
            )
            if len(fascist_tweets) >= 5:
                return web.json_response(
                    {
                        "message": "You've liked too many fascist tweets to be allowed to automatically unblock yourself"
                    }
                )
            else:
                # They liked few enough fascist tweets, so create an unblock job
                await UnblockJob.create(
                    user_id=user.id,
                    twitter_username=user.twitter_screen_name,
                    status="pending",
                    scheduled_timestamp=datetime.now(),
                )
                return web.json_response(
                    {"message": "You should be unblocked in the next few minutes"}
                )
        else:
            # The user is already unblocked, so update them in the db
            await user.update(blocked=False, since_id=None).apply()

            return web.json_response({"message": "You are already unblocked"})

    if data["action"] == "reactivate":
        if not user.blocked:
            raise web.HTTPBadRequest(
                text="Can only 'reactivate' if the user is blocked"
            )

        # Are we still blocked?
        api = await twitter_api(user)
        friendship = (
            await twitter_api_call(
                api,
                "show_friendship",
                source_id=user.twitter_id,
                target_screen_name="semiphemeral",
            )
        )[0]

        if friendship.blocked_by:
            return web.json_response({"unblocked": False})
        else:
            # Delete the user's likes so we can start over and check them all
            await Tweet.delete.where(Tweet.user_id == user.id).where(
                Tweet.favorited == True
            ).gino.status()

            # User has been unblocked
            await user.update(blocked=False, since_id=None).apply()

            # Create a new fetch job
            await Job.create(
                user_id=user.id,
                job_type="fetch",
                status="pending",
                scheduled_timestamp=datetime.now(),
            )

            return web.json_response({"unblocked": True})

    else:
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

            # Unpause
            await user.update(paused=False).apply()

            # Create a new delete job
            await Job.create(
                user_id=user.id,
                job_type="delete",
                status="pending",
                scheduled_timestamp=datetime.now(),
            )

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

        elif data["action"] == "fetch":
            if not user.paused:
                raise web.HTTPBadRequest(
                    text="Cannot 'fetch' unless semiphemeral is paused"
                )

            if len(jobs) > 0:
                raise web.HTTPBadRequest(
                    text="Cannot 'fetch' when there are pending or active jobs"
                )

            # Create a new fetch job
            await Job.create(
                user_id=user.id,
                job_type="fetch",
                status="pending",
                scheduled_timestamp=datetime.now(),
            )

        return web.json_response(True)


@authentication_required_401
async def api_get_tweets(request):
    """
    Respond with the current user's list of tweets
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    tweets_for_client = []
    for tweet in (
        await Tweet.query.where(Tweet.user_id == user.id)
        .where(Tweet.twitter_user_id == user.twitter_id)
        .where(Tweet.is_deleted == False)
        .where(Tweet.is_retweet == False)
        .order_by(Tweet.created_at.desc())
        .gino.all()
    ):
        created_at = tweet.created_at.timestamp()
        is_reply = tweet.in_reply_to_status_id is not None
        tweets_for_client.append(
            {
                "created_at": created_at,
                "status_id": str(
                    tweet.status_id
                ),  # Typecast it to a string, to avoid javascript issues
                "text": tweet.text,
                "is_reply": is_reply,
                "retweet_count": tweet.retweet_count,
                "like_count": tweet.favorite_count,
                "exclude": tweet.exclude_from_delete,
            }
        )

    return web.json_response({"tweets": tweets_for_client})


@authentication_required_401
async def api_post_tweets(request):
    """
    Toggle "exclude for deletion" on a tweet
    """
    session = await get_session(request)
    user = await _logged_in_user(session)
    data = await request.json()

    # Validate
    await _api_validate({"status_id": str, "exclude": bool}, data)
    tweet = (
        await Tweet.query.where(Tweet.user_id == user.id)
        .where(Tweet.twitter_user_id == user.twitter_id)
        .where(Tweet.status_id == int(data["status_id"]))
        .gino.first()
    )
    if not tweet:
        raise web.HTTPBadRequest(text="Invalid status_id")

    # Update exclude from delete
    await tweet.update(exclude_from_delete=data["exclude"]).apply()

    return web.json_response(True)


@authentication_required_401
async def api_get_dms(request):
    """
    Get information about deleting DMs
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    is_dm_app_authenticated = await _api_validate_dms_authenticated(user)

    job = (
        await Job.query.where(Job.user_id == user.id)
        .where(or_(Job.job_type == "delete_dms", Job.job_type == "delete_dm_groups"))
        .where(or_(Job.status == "pending", Job.status == "active"))
        .gino.first()
    )
    is_dm_job_ongoing = job is not None

    return web.json_response(
        {
            "direct_messages": user.direct_messages,
            "is_dm_app_authenticated": is_dm_app_authenticated,
            "is_dm_job_ongoing": is_dm_job_ongoing,
        }
    )


@authentication_required_401
async def api_post_dms(request):
    """
    Upload a direct-message-headers.js file to bulk delete old DMs
    """
    session = await get_session(request)
    user = await _logged_in_user(session)

    if not await _api_validate_dms_authenticated(user):
        return web.json_response(
            {
                "error": True,
                "error_message": "You are not authenticated to the Semiphemeral DMs Twitter app",
            }
        )
    if not user.direct_messages:
        return web.json_response(
            {
                "error": True,
                "error_message": "You have not enabled deleting direct messages in your settings",
            }
        )

    # Validate
    post = await request.post()
    dms_file = post.get("file")
    if not dms_file:
        return web.json_response(
            {
                "error": True,
                "error_message": "Uploading file failed",
            }
        )

    # Detect if this is direct-message-headers.js or direct-message-group-headers.js
    expected_dm_start = b"window.YTD.direct_message_headers.part0 = "
    expected_dm_group_start = b"window.YTD.direct_message_group_headers.part0 = "

    content = dms_file.file.read()
    if content.startswith(expected_dm_start):
        dm_type = "dms"
        json_string = content[len(expected_dm_start) :]
    elif content.startswith(expected_dm_group_start):
        dm_type = "groups"
        json_string = content[len(expected_dm_group_start) :]
    else:
        return web.json_response(
            {
                "error": True,
                "error_message": "This does not appear to be a direct-message-headers.js or direct-message-group-headers.js file",
            }
        )

    try:
        conversations = json.loads(json_string)
    except:
        return web.json_response(
            {
                "error": True,
                "error_message": "Failed parsing JSON object",
            }
        )

    if type(conversations) != list:
        return web.json_response(
            {
                "error": True,
                "error_message": "JSON object expected to be a list",
            }
        )

    for obj in conversations:
        if type(obj) != dict:
            return web.json_response(
                {
                    "error": True,
                    "error_message": "JSON object expected to be a list of dicts",
                }
            )
        if "dmConversation" not in obj:
            return web.json_response(
                {
                    "error": True,
                    "error_message": "JSON object expected to be a list of dicts that contain 'dmConversation' fields",
                }
            )
        dm_conversation = obj["dmConversation"]
        if "messages" not in dm_conversation:
            return web.json_response(
                {
                    "error": True,
                    "error_message": "JSON object expected to be a list of dicts that contain 'dmConversations' fields that contain 'messages' fields",
                }
            )

    # Save to disk
    if dm_type == "dms":
        job_type = "delete_dms"
        filename = os.path.join("/var/bulk_dms", f"dms-{user.id}.json")
    elif dm_type == "groups":
        job_type = "delete_dm_groups"
        filename = os.path.join("/var/bulk_dms", f"groups-{user.id}.json")
    with open(filename, "w") as f:
        f.write(json.dumps(conversations, indent=2))

    # Create a new delete_dms job
    await Job.create(
        user_id=user.id,
        job_type=job_type,
        status="pending",
        scheduled_timestamp=datetime.now(),
    )

    return web.json_response({"error": False})


@aiohttp_jinja2.template("index.jinja2")
async def index(request):
    session = await get_session(request)
    user = await _logged_in_user(session)
    logged_in = user is not None
    return {"logged_in": logged_in}


@aiohttp_jinja2.template("privacy.jinja2")
async def privacy(request):
    return {}


@aiohttp_jinja2.template("app.jinja2")
@authentication_required_302
async def app_main(request):
    return {"deploy_environment": os.environ.get("DEPLOY_ENVIRONMENT")}


@aiohttp_jinja2.template("admin.jinja2")
@admin_required
async def app_admin(request):
    return {"deploy_environment": os.environ.get("DEPLOY_ENVIRONMENT")}


@admin_required
async def app_admin_redirect(request):
    raise web.HTTPFound(location="/admin/jobs")


@admin_required
async def admin_api_get_jobs(request):
    active_jobs = (
        await Job.query.where(Job.status == "active")
        .order_by(Job.started_timestamp)
        .gino.all()
    )

    queued_jobs = (
        await Job.query.where(Job.status == "queued")
        .where(Job.scheduled_timestamp <= datetime.now())
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    pending_jobs = (
        await Job.query.where(Job.status == "pending")
        .where(Job.scheduled_timestamp <= datetime.now())
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    future_jobs = (
        await Job.query.where(Job.status == "pending")
        .where(Job.scheduled_timestamp > datetime.now())
        .order_by(Job.scheduled_timestamp)
        .gino.all()
    )

    async def to_client(jobs):
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
            if job.finished_timestamp:
                finished_timestamp = job.finished_timestamp.timestamp()
            else:
                finished_timestamp = None
            if job.container_name:
                container_name = job.container_name
            else:
                container_name = "null"

            user = await User.query.where(User.id == job.user_id).gino.first()
            if user:
                twitter_username = user.twitter_screen_name
                twitter_link = f"https://twitter.com/{user.twitter_screen_name}"
            else:
                twitter_username = None
                twitter_link = None

            jobs_json.append(
                {
                    "id": job.id,
                    "user_id": job.user_id,
                    "twitter_username": twitter_username,
                    "twitter_link": twitter_link,
                    "job_type": job.job_type,
                    "progress": job.progress,
                    "status": job.status,
                    "scheduled_timestamp": scheduled_timestamp,
                    "started_timestamp": started_timestamp,
                    "container_name": container_name,
                }
            )
        return jobs_json

    return web.json_response(
        {
            "active_jobs": await to_client(active_jobs),
            "queued_jobs": await to_client(queued_jobs),
            "pending_jobs": await to_client(pending_jobs),
            "future_jobs": await to_client(future_jobs),
        }
    )


@admin_required
async def admin_api_get_users(request):
    users = await User.query.order_by(User.twitter_screen_name).gino.all()
    active_users = []
    paused_users = []
    blocked_users = []

    for user in users:
        if user.blocked:
            blocked_users.append(user)
        else:
            if user.paused:
                paused_users.append(user)
            else:
                active_users.append(user)

    def to_client(users):
        users_json = []
        for user in users:
            users_json.append(
                {
                    "id": user.id,
                    "twitter_id": user.twitter_id,
                    "twitter_screen_name": user.twitter_screen_name,
                    "blocked": user.blocked,
                }
            )
        return users_json

    session = await get_session(request)

    impersonating_twitter_id = None
    impersonating_twitter_username = None

    if "impersonating_twitter_id" in session:
        impersonating_user = await User.query.where(
            User.twitter_id == session["impersonating_twitter_id"]
        ).gino.first()
        if impersonating_user:
            impersonating_twitter_id = session["impersonating_twitter_id"]
            impersonating_twitter_username = impersonating_user.twitter_screen_name

    return web.json_response(
        {
            "impersonating_twitter_id": impersonating_twitter_id,
            "impersonating_twitter_username": impersonating_twitter_username,
            "active_users": to_client(active_users),
            "paused_users": to_client(paused_users),
            "blocked_users": to_client(blocked_users),
        }
    )


@admin_required
async def admin_api_get_user(request):
    user_id = int(request.match_info["user_id"])
    user = await User.query.where(User.id == user_id).gino.first()
    if not user:
        return web.json_response(False)

    # Get fascist tweets that this user has liked
    fascist_tweets = (
        await Tweet.query.where(Tweet.user_id == user_id)
        .where(Tweet.favorited == True)
        .where(Tweet.is_fascist == True)
        .order_by(Tweet.created_at.desc())
        .gino.all()
    )
    fascist_tweet_urls = [
        f"https://twitter.com/{tweet.twitter_user_screen_name}/status/{tweet.status_id}"
        for tweet in fascist_tweets
    ]

    return web.json_response(
        {
            "twitter_username": user.twitter_screen_name,
            "paused": user.paused,
            "blocked": user.blocked,
            "fascist_tweet_urls": fascist_tweet_urls,
        }
    )


@admin_required
async def admin_api_post_user_impersonate(request):
    session = await get_session(request)
    data = await request.json()

    # Validate
    await _api_validate({"twitter_id": str}, data)
    if data["twitter_id"] == "0":
        del session["impersonating_twitter_id"]
    else:
        impersonating_user = await User.query.where(
            User.twitter_id == data["twitter_id"]
        ).gino.first()
        if impersonating_user:
            session["impersonating_twitter_id"] = data["twitter_id"]

    return web.json_response(True)


@admin_required
async def admin_api_get_fascists(request):
    fascists = await Fascist.query.order_by(Fascist.username).gino.all()

    def to_client(fascists):
        fascists_json = []
        for fascist in fascists:
            fascists_json.append(
                {
                    "username": fascist.username,
                    "comment": fascist.comment,
                }
            )
        return fascists_json

    return web.json_response({"fascists": to_client(fascists)})


@admin_required
async def admin_api_post_fascists(request):
    data = await request.json()

    # Validate
    await _api_validate({"action": str}, data)
    if data["action"] != "create" and data["action"] != "delete":
        raise web.HTTPBadRequest(text="action must be 'create' or 'delete'")

    if data["action"] == "create":
        await _api_validate({"action": str, "username": str, "comment": str}, data)

        # If a fascist with this username already exists, just update the comment
        fascist = await Fascist.query.where(
            Fascist.username == data["username"]
        ).gino.first()
        if fascist:
            await fascist.update(comment=data["comment"]).apply()
            return web.json_response(True)

        # Create the fascist
        fascist = await Fascist.create(
            username=data["username"], comment=data["comment"]
        )

        # Mark all the tweets from this user as is_fascist=True
        await Tweet.update.values(is_fascist=True).where(
            Tweet.twitter_user_screen_name.ilike(data["username"])
        ).gino.status()

        # Make sure the facist is blocked
        await BlockJob.create(
            twitter_username=data["username"],
            status="pending",
            scheduled_timestamp=datetime.now(),
        )

        return web.json_response(True)

    elif data["action"] == "delete":
        await _api_validate({"action": str, "username": str}, data)

        # Delete the fascist
        fascist = await Fascist.query.where(
            Fascist.username == data["username"]
        ).gino.first()
        if fascist:
            await fascist.delete()

        # Mark all the tweets from this user as is_fascist=False
        await Tweet.update.values(is_fascist=False).where(
            Tweet.twitter_user_screen_name == data["username"]
        ).gino.status()

        return web.json_response(True)


@admin_required
async def admin_api_get_tips(request):
    users = {}
    tips = await Tip.query.order_by(Tip.timestamp.desc()).gino.all()

    for tip in tips:
        if tip.user_id not in users:
            user = await User.query.where(User.id == tip.user_id).gino.first()
            if user:
                users[tip.user_id] = {
                    "twitter_username": user.twitter_screen_name,
                    "twitter_link": f"https://twitter.com/{user.twitter_screen_name}",
                }
            else:
                users[tip.user_id] = {"twitter_username": "", "twitter_link": ""}

    def to_client(tips):
        tips_json = []
        for tip in tips:
            tips_json.append(
                {
                    "twitter_username": users[tip.user_id]["twitter_username"],
                    "twitter_link": users[tip.user_id]["twitter_link"],
                    "timestamp": tip.timestamp.timestamp(),
                    "amount": tip.amount,
                    "paid": tip.paid,
                    "refunded": tip.refunded,
                    "receipt_url": tip.receipt_url,
                }
            )
        return tips_json

    return web.json_response({"tips": to_client(tips)})


async def maintenance_refresh_logging(request=None):
    """
    Refreshes logging. This needs to get run after rotating logs, to re-open the
    web.log file
    """
    logging.basicConfig(filename="/var/web/web.log", level=logging.INFO, force=True)

    if request:
        return web.json_response(True)


async def start_web_server():
    # If staging, start by pausing all users and cancel all pending jobs
    if os.environ.get("DEPLOY_ENVIRONMENT") == "staging":
        print("Staging environment, so pausing all users and canceling all jobs")
        await User.update.values(paused=True).gino.status()
        await Job.update.values(status="canceled").where(
            Job.status == "pending"
        ).gino.status()
        await DirectMessageJob.update.values(status="canceled").where(
            DirectMessageJob.status == "pending"
        ).gino.status()
        await BlockJob.update.values(status="canceled").where(
            BlockJob.status == "pending"
        ).gino.status()
        await UnblockJob.update.values(status="canceled").where(
            UnblockJob.status == "pending"
        ).gino.status()

    # Send admin DM now, so it doesn't get immediately canceled in staging
    await send_admin_dm(
        f"web server container started ({os.environ.get('DEPLOY_ENVIRONMENT')})"
    )

    # Init stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

    # Create the web app
    app = web.Application()
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader("templates"))
    await maintenance_refresh_logging()

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
            web.get("/auth/twitter_dms_callback", auth_twitter_dms_callback),
            # Stripe
            web.post("/stripe/callback", stripe_callback),
            # API
            web.get("/api/user", api_get_user),
            web.get("/api/settings", api_get_settings),
            web.post("/api/settings", api_post_settings),
            web.post("/api/settings/delete_account", api_post_settings_delete_account),
            web.get("/api/tip", api_get_tip),
            web.post("/api/tip", api_post_tip),
            web.get("/api/tip/recent", api_get_tip_recent),
            web.get("/api/tip/history", api_get_tip_history),
            web.get("/api/dashboard", api_get_dashboard),
            web.post("/api/dashboard", api_post_dashboard),
            web.get("/api/tweets", api_get_tweets),
            web.post("/api/tweets", api_post_tweets),
            web.get("/api/dms", api_get_dms),
            web.post("/api/dms", api_post_dms),
            # Web
            web.get("/", index),
            web.get("/privacy", privacy),
            web.get("/dashboard", app_main),
            web.get("/tweets", app_main),
            web.get("/export", app_main),
            web.get("/export/download", api_get_export_download),
            web.get("/dms", app_main),
            web.get("/settings", app_main),
            web.get("/tip", app_main),
            web.get("/thanks", app_main),
            web.get("/faq", app_main),
            # Admin
            web.get("/admin", app_admin_redirect),
            web.get("/admin/jobs", app_admin),
            web.get("/admin/users", app_admin),
            web.get("/admin/fascists", app_admin),
            web.get("/admin/tips", app_admin),
            # Admin API
            web.get("/admin_api/jobs", admin_api_get_jobs),
            web.get("/admin_api/users", admin_api_get_users),
            web.get("/admin_api/users/{user_id}", admin_api_get_user),
            web.post("/admin_api/users/impersonate", admin_api_post_user_impersonate),
            web.get("/admin_api/fascists", admin_api_get_fascists),
            web.post("/admin_api/fascists", admin_api_post_fascists),
            web.get("/admin_api/tips", admin_api_get_tips),
            # Maintenance
            web.get(
                f"/{os.environ.get('MAINTENANCE_SECRET')}/refresh_logging",
                maintenance_refresh_logging,
            ),
        ]
    )

    loop = asyncio.get_event_loop()
    server = await loop.create_server(app.make_handler(), port=8080)
    await server.serve_forever()

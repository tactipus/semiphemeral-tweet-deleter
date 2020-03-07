import os
import asyncio
import functools
import tweepy
from datetime import datetime, timedelta

from db import Tweet, Thread


class TwitterRateLimit(Exception):
    pass


async def twitter_api_call(api, method, **kwargs):
    """
    Wrapper around Twitter API to support asyncio for all API calls. See:
    https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor
    https://docs.python.org/3/library/asyncio-eventloop.html#asyncio-pass-keywords
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, functools.partial(getattr(api, method), **kwargs)
    )
    return result


async def twitter_api(user):
    auth = tweepy.OAuthHandler(
        os.environ.get("TWITTER_CONSUMER_TOKEN"),
        os.environ.get("TWITTER_CONSUMER_KEY"),
    )
    auth.set_access_token(user.twitter_access_token, user.twitter_access_token_secret)
    api = tweepy.API(auth)
    return api


async def twitter_dm_api():
    auth = tweepy.OAuthHandler(
        os.environ.get("TWITTER_DM_CONSUMER_TOKEN"),
        os.environ.get("TWITTER_DM_CONSUMER_KEY"),
    )
    auth.set_access_token(
        os.environ.get("TWITTER_DM_ACCESS_TOKEN"),
        os.environ.get("TWITTER_DM_ACCESS_KEY"),
    )
    api = tweepy.API(auth)
    return api


async def tweets_to_delete(user, include_manually_excluded=False):
    """
    Return the tweets that are staged for deletion for this user
    """
    datetime_threshold = datetime.utcnow() - timedelta(days=user.tweets_days_threshold)

    # Get all the tweets to delete that have threads
    query = (
        Tweet.query.select_from(Tweet.join(Thread))
        .where(Tweet.user_id == user.id)
        .where(Tweet.twitter_user_id == user.twitter_id)
        .where(Tweet.is_deleted == False)
        .where(Tweet.is_retweet == False)
        .where(Tweet.created_at < datetime_threshold)
        .where(Tweet.retweet_count < user.tweets_retweet_threshold)
        .where(Tweet.favorite_count < user.tweets_like_threshold)
        .where(Thread.should_exclude == False)
    )
    if not include_manually_excluded:
        query = query.where(Tweet.exclude_from_delete == False)
    tweets_to_delete_with_threads = await query.gino.all()

    # Get all the tweets to delete that don't have threads
    query = (
        Tweet.query.where(Tweet.thread_id == None)
        .where(Tweet.user_id == user.id)
        .where(Tweet.twitter_user_id == user.twitter_id)
        .where(Tweet.is_deleted == False)
        .where(Tweet.is_retweet == False)
        .where(Tweet.created_at < datetime_threshold)
        .where(Tweet.retweet_count < user.tweets_retweet_threshold)
        .where(Tweet.favorite_count < user.tweets_like_threshold)
    )
    if not include_manually_excluded:
        query = query.where(Tweet.exclude_from_delete == False)
    tweets_to_delete_without_threads = await query.gino.all()

    # Merge them
    tweets_to_delete = sorted(tweets_to_delete_with_threads + tweets_to_delete_without_threads, key=lambda tweet: tweet.created_at)

    return tweets_to_delete

''' handles all the activity coming out of the server '''
import re

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from markdown import markdown
from requests import HTTPError

from bookwyrm import activitypub
from bookwyrm import models
from bookwyrm.connectors import get_data, ConnectorException
from bookwyrm.broadcast import broadcast
from bookwyrm.sanitize_html import InputHtmlParser
from bookwyrm.status import create_notification
from bookwyrm.status import create_generated_note
from bookwyrm.status import delete_status
from bookwyrm.settings import DOMAIN
from bookwyrm.utils import regex


@csrf_exempt
@require_GET
def outbox(request, username):
    ''' outbox for the requested user '''
    user = get_object_or_404(models.User, localname=username)
    filter_type = request.GET.get('type')
    if filter_type not in models.status_models:
        filter_type = None

    return JsonResponse(
        user.to_outbox(**request.GET, filter_type=filter_type),
        encoder=activitypub.ActivityEncoder
    )


def handle_remote_webfinger(query):
    ''' webfingerin' other servers '''
    user = None

    # usernames could be @user@domain or user@domain
    if not query:
        return None

    if query[0] == '@':
        query = query[1:]

    try:
        domain = query.split('@')[1]
    except IndexError:
        return None

    try:
        user = models.User.objects.get(username=query)
    except models.User.DoesNotExist:
        url = 'https://%s/.well-known/webfinger?resource=acct:%s' % \
            (domain, query)
        try:
            data = get_data(url)
        except (ConnectorException, HTTPError):
            return None

        for link in data.get('links'):
            if link.get('rel') == 'self':
                try:
                    user = activitypub.resolve_remote_id(
                        models.User, link['href']
                    )
                except KeyError:
                    return None
    return user


def handle_follow(user, to_follow):
    ''' someone local wants to follow someone '''
    relationship, _ = models.UserFollowRequest.objects.get_or_create(
        user_subject=user,
        user_object=to_follow,
    )
    activity = relationship.to_activity()
    broadcast(user, activity, privacy='direct', direct_recipients=[to_follow])


def handle_unfollow(user, to_unfollow):
    ''' someone local wants to follow someone '''
    relationship = models.UserFollows.objects.get(
        user_subject=user,
        user_object=to_unfollow
    )
    activity = relationship.to_undo_activity(user)
    broadcast(user, activity, privacy='direct', direct_recipients=[to_unfollow])
    to_unfollow.followers.remove(user)


def handle_accept(follow_request):
    ''' send an acceptance message to a follow request '''
    user = follow_request.user_subject
    to_follow = follow_request.user_object
    with transaction.atomic():
        relationship = models.UserFollows.from_request(follow_request)
        follow_request.delete()
        relationship.save()

    activity = relationship.to_accept_activity()
    broadcast(to_follow, activity, privacy='direct', direct_recipients=[user])


def handle_reject(follow_request):
    ''' a local user who managed follows rejects a follow request '''
    user = follow_request.user_subject
    to_follow = follow_request.user_object
    activity = follow_request.to_reject_activity()
    follow_request.delete()
    broadcast(to_follow, activity, privacy='direct', direct_recipients=[user])


def handle_shelve(user, book, shelf):
    ''' a local user is getting a book put on their shelf '''
    # update the database
    shelve = models.ShelfBook(book=book, shelf=shelf, added_by=user)
    shelve.save()

    broadcast(user, shelve.to_add_activity(user))


def handle_unshelve(user, book, shelf):
    ''' a local user is getting a book put on their shelf '''
    # update the database
    row = models.ShelfBook.objects.get(book=book, shelf=shelf)
    activity = row.to_remove_activity(user)
    row.delete()

    broadcast(user, activity)


def handle_reading_status(user, shelf, book, privacy):
    ''' post about a user reading a book '''
    # tell the world about this cool thing that happened
    try:
        message = {
            'to-read': 'wants to read',
            'reading': 'started reading',
            'read': 'finished reading'
        }[shelf.identifier]
    except KeyError:
        # it's a non-standard shelf, don't worry about it
        return

    status = create_generated_note(
        user,
        message,
        mention_books=[book],
        privacy=privacy
    )
    status.save()

    broadcast(user, status.to_create_activity(user))


def handle_imported_book(user, item, include_reviews, privacy):
    ''' process a goodreads csv and then post about it '''
    if isinstance(item.book, models.Work):
        item.book = item.book.default_edition
    if not item.book:
        return

    if item.shelf:
        desired_shelf = models.Shelf.objects.get(
            identifier=item.shelf,
            user=user
        )
        # shelve the book if it hasn't been shelved already
        shelf_book, created = models.ShelfBook.objects.get_or_create(
            book=item.book, shelf=desired_shelf, added_by=user)
        if created:
            broadcast(user, shelf_book.to_add_activity(user), privacy=privacy)

            # only add new read-throughs if the item isn't already shelved
            for read in item.reads:
                read.book = item.book
                read.user = user
                read.save()

    if include_reviews and (item.rating or item.review):
        review_title = 'Review of {!r} on Goodreads'.format(
            item.book.title,
        ) if item.review else ''

        # we don't know the publication date of the review,
        # but "now" is a bad guess
        published_date_guess = item.date_read or item.date_added
        review = models.Review.objects.create(
            user=user,
            book=item.book,
            name=review_title,
            content=item.review,
            rating=item.rating,
            published_date=published_date_guess,
            privacy=privacy,
        )
        # we don't need to send out pure activities because non-bookwyrm
        # instances don't need this data
        broadcast(user, review.to_create_activity(user), privacy=privacy)


def handle_delete_status(user, status):
    ''' delete a status and broadcast deletion to other servers '''
    delete_status(status)
    broadcast(user, status.to_delete_activity(user))


def handle_status(user, form):
    ''' generic handler for statuses '''
    status = form.save(commit=False)
    if not status.sensitive and status.content_warning:
        # the cw text field remains populated when you click "remove"
        status.content_warning = None
    status.save()

    # inspect the text for user tags
    matches = []
    for match in re.finditer(regex.strict_username, status.content):
        username = match.group().strip().split('@')[1:]
        if len(username) == 1:
            # this looks like a local user (@user), fill in the domain
            username.append(DOMAIN)
        username = '@'.join(username)

        mention_user = handle_remote_webfinger(username)
        if not mention_user:
            # we can ignore users we don't know about
            continue
        matches.append((match.group(), mention_user.remote_id))
        # add them to status mentions fk
        status.mention_users.add(mention_user)
        # create notification if the mentioned user is local
        if mention_user.local:
            create_notification(
                mention_user,
                'MENTION',
                related_user=user,
                related_status=status
            )
    # add mentions
    content = status.content
    for (username, url) in matches:
        content = re.sub(
            r'%s([^@])' % username,
            r'<a href="%s">%s</a>\g<1>' % (url, username),
            content)
    if not isinstance(status, models.GeneratedNote):
        status.content = to_markdown(content)
    if hasattr(status, 'quote'):
        status.quote = to_markdown(status.quote)
    status.save()

    # notify reply parent or tagged users
    if status.reply_parent and status.reply_parent.user.local:
        create_notification(
            status.reply_parent.user,
            'REPLY',
            related_user=user,
            related_status=status
        )

    broadcast(user, status.to_create_activity(user), software='bookwyrm')

    # re-format the activity for non-bookwyrm servers
    remote_activity = status.to_create_activity(user, pure=True)
    broadcast(user, remote_activity, software='other')


def to_markdown(content):
    ''' catch links and convert to markdown '''
    content = re.sub(
        r'([^(href=")])(https?:\/\/([A-Za-z\.\-_\/]+' \
            r'\.[A-Za-z]{2,}[A-Za-z\.\-_\/]+))',
        r'\g<1><a href="\g<2>">\g<3></a>',
        content)
    content = markdown(content)
    # sanitize resulting html
    sanitizer = InputHtmlParser()
    sanitizer.feed(content)
    return sanitizer.get_output()


def handle_favorite(user, status):
    ''' a user likes a status '''
    try:
        favorite = models.Favorite.objects.create(
            status=status,
            user=user
        )
    except IntegrityError:
        # you already fav'ed that
        return

    fav_activity = favorite.to_activity()
    broadcast(
        user, fav_activity, privacy='direct', direct_recipients=[status.user])
    create_notification(
        status.user,
        'FAVORITE',
        related_user=user,
        related_status=status
    )


def handle_unfavorite(user, status):
    ''' a user likes a status '''
    try:
        favorite = models.Favorite.objects.get(
            status=status,
            user=user
        )
    except models.Favorite.DoesNotExist:
        # can't find that status, idk
        return

    fav_activity = favorite.to_undo_activity(user)
    favorite.delete()
    broadcast(user, fav_activity, direct_recipients=[status.user])


def handle_boost(user, status):
    ''' a user wishes to boost a status '''
    # is it boostable?
    if not status.boostable:
        return

    if models.Boost.objects.filter(
            boosted_status=status, user=user).exists():
        # you already boosted that.
        return
    boost = models.Boost.objects.create(
        boosted_status=status,
        privacy=status.privacy,
        user=user,
    )

    boost_activity = boost.to_activity()
    broadcast(user, boost_activity)

    create_notification(
        status.user,
        'BOOST',
        related_user=user,
        related_status=status
    )


def handle_unboost(user, status):
    ''' a user regrets boosting a status '''
    boost = models.Boost.objects.filter(
        boosted_status=status, user=user
    ).first()
    activity = boost.to_undo_activity(user)

    boost.delete()
    broadcast(user, activity)


def handle_update_book_data(user, item):
    ''' broadcast the news about our book '''
    broadcast(user, item.to_update_activity(user))


def handle_update_user(user):
    ''' broadcast editing a user's profile '''
    broadcast(user, user.to_update_activity(user))

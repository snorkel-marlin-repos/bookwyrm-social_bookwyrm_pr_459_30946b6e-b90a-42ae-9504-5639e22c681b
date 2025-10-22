''' sending out activities '''
import json
import pathlib
from unittest.mock import patch

from django.http import JsonResponse
from django.test import TestCase
from django.test.client import RequestFactory
import responses

from bookwyrm import models, outgoing
from bookwyrm.settings import DOMAIN


class Outgoing(TestCase):
    ''' sends out activities '''
    def setUp(self):
        ''' we'll need some data '''
        self.factory = RequestFactory()
        with patch('bookwyrm.models.user.set_remote_server'):
            self.remote_user = models.User.objects.create_user(
                'rat', 'rat@rat.com', 'ratword',
                local=False,
                remote_id='https://example.com/users/rat',
                inbox='https://example.com/users/rat/inbox',
                outbox='https://example.com/users/rat/outbox',
            )
        self.local_user = models.User.objects.create_user(
            'mouse', 'mouse@mouse.com', 'mouseword', local=True,
            localname='mouse', remote_id='https://example.com/users/mouse',
        )

        datafile = pathlib.Path(__file__).parent.joinpath(
            'data/ap_user.json'
        )
        self.userdata = json.loads(datafile.read_bytes())
        del self.userdata['icon']

        work = models.Work.objects.create(title='Test Work')
        self.book = models.Edition.objects.create(
            title='Example Edition',
            remote_id='https://example.com/book/1',
            parent_work=work
        )
        self.shelf = models.Shelf.objects.create(
            name='Test Shelf',
            identifier='test-shelf',
            user=self.local_user
        )


    def test_outbox(self):
        ''' returns user's statuses '''
        request = self.factory.get('')
        result = outgoing.outbox(request, 'mouse')
        self.assertIsInstance(result, JsonResponse)

    def test_outbox_bad_method(self):
        ''' can't POST to outbox '''
        request = self.factory.post('')
        result = outgoing.outbox(request, 'mouse')
        self.assertEqual(result.status_code, 405)

    def test_outbox_unknown_user(self):
        ''' should 404 for unknown and remote users '''
        request = self.factory.post('')
        result = outgoing.outbox(request, 'beepboop')
        self.assertEqual(result.status_code, 405)
        result = outgoing.outbox(request, 'rat')
        self.assertEqual(result.status_code, 405)

    def test_outbox_privacy(self):
        ''' don't show dms et cetera in outbox '''
        models.Status.objects.create(
            content='PRIVATE!!', user=self.local_user, privacy='direct')
        models.Status.objects.create(
            content='bffs ONLY', user=self.local_user, privacy='followers')
        models.Status.objects.create(
            content='unlisted status', user=self.local_user, privacy='unlisted')
        models.Status.objects.create(
            content='look at this', user=self.local_user, privacy='public')

        request = self.factory.get('')
        result = outgoing.outbox(request, 'mouse')
        self.assertIsInstance(result, JsonResponse)
        data = json.loads(result.content)
        self.assertEqual(data['type'], 'OrderedCollection')
        self.assertEqual(data['totalItems'], 2)

    def test_outbox_filter(self):
        ''' if we only care about reviews, only get reviews '''
        models.Review.objects.create(
            content='look at this', name='hi', rating=1,
            book=self.book, user=self.local_user)
        models.Status.objects.create(
            content='look at this', user=self.local_user)

        request = self.factory.get('', {'type': 'bleh'})
        result = outgoing.outbox(request, 'mouse')
        self.assertIsInstance(result, JsonResponse)
        data = json.loads(result.content)
        self.assertEqual(data['type'], 'OrderedCollection')
        self.assertEqual(data['totalItems'], 2)

        request = self.factory.get('', {'type': 'Review'})
        result = outgoing.outbox(request, 'mouse')
        self.assertIsInstance(result, JsonResponse)
        data = json.loads(result.content)
        self.assertEqual(data['type'], 'OrderedCollection')
        self.assertEqual(data['totalItems'], 1)


    def test_handle_follow(self):
        ''' send a follow request '''
        self.assertEqual(models.UserFollowRequest.objects.count(), 0)

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_follow(self.local_user, self.remote_user)

        rel = models.UserFollowRequest.objects.get()

        self.assertEqual(rel.user_subject, self.local_user)
        self.assertEqual(rel.user_object, self.remote_user)
        self.assertEqual(rel.status, 'follow_request')


    def test_handle_unfollow(self):
        ''' send an unfollow '''
        self.remote_user.followers.add(self.local_user)
        self.assertEqual(self.remote_user.followers.count(), 1)
        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_unfollow(self.local_user, self.remote_user)

        self.assertEqual(self.remote_user.followers.count(), 0)


    def test_handle_accept(self):
        ''' accept a follow request '''
        rel = models.UserFollowRequest.objects.create(
            user_subject=self.local_user,
            user_object=self.remote_user
        )
        rel_id = rel.id

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_accept(rel)
        # request should be deleted
        self.assertEqual(
            models.UserFollowRequest.objects.filter(id=rel_id).count(), 0
        )
        # follow relationship should exist
        self.assertEqual(self.remote_user.followers.first(), self.local_user)


    def test_handle_reject(self):
        ''' reject a follow request '''
        rel = models.UserFollowRequest.objects.create(
            user_subject=self.local_user,
            user_object=self.remote_user
        )
        rel_id = rel.id

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_reject(rel)
        # request should be deleted
        self.assertEqual(
            models.UserFollowRequest.objects.filter(id=rel_id).count(), 0
        )
        # follow relationship should not exist
        self.assertEqual(
            models.UserFollows.objects.filter(id=rel_id).count(), 0
        )

    def test_existing_user(self):
        ''' simple database lookup by username '''
        result = outgoing.handle_remote_webfinger('@mouse@%s' % DOMAIN)
        self.assertEqual(result, self.local_user)

        result = outgoing.handle_remote_webfinger('mouse@%s' % DOMAIN)
        self.assertEqual(result, self.local_user)


    @responses.activate
    def test_load_user(self):
        ''' find a remote user using webfinger '''
        username = 'mouse@example.com'
        wellknown = {
            "subject": "acct:mouse@example.com",
            "links": [{
                "rel": "self",
                "type": "application/activity+json",
                "href": "https://example.com/user/mouse"
            }]
        }
        responses.add(
            responses.GET,
            'https://example.com/.well-known/webfinger?resource=acct:%s' \
                    % username,
            json=wellknown,
            status=200)
        responses.add(
            responses.GET,
            'https://example.com/user/mouse',
            json=self.userdata,
            status=200)
        with patch('bookwyrm.models.user.set_remote_server.delay'):
            result = outgoing.handle_remote_webfinger('@mouse@example.com')
            self.assertIsInstance(result, models.User)
            self.assertEqual(result.username, 'mouse@example.com')


    def test_handle_shelve(self):
        ''' shelve a book '''
        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_shelve(self.local_user, self.book, self.shelf)
        # make sure the book is on the shelf
        self.assertEqual(self.shelf.books.get(), self.book)


    def test_handle_shelve_to_read(self):
        ''' special behavior for the to-read shelf '''
        shelf = models.Shelf.objects.get(identifier='to-read')

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_shelve(self.local_user, self.book, shelf)
        # make sure the book is on the shelf
        self.assertEqual(shelf.books.get(), self.book)


    def test_handle_shelve_reading(self):
        ''' special behavior for the reading shelf '''
        shelf = models.Shelf.objects.get(identifier='reading')

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_shelve(self.local_user, self.book, shelf)
        # make sure the book is on the shelf
        self.assertEqual(shelf.books.get(), self.book)


    def test_handle_shelve_read(self):
        ''' special behavior for the read shelf '''
        shelf = models.Shelf.objects.get(identifier='read')

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_shelve(self.local_user, self.book, shelf)
        # make sure the book is on the shelf
        self.assertEqual(shelf.books.get(), self.book)


    def test_handle_unshelve(self):
        ''' remove a book from a shelf '''
        self.shelf.books.add(self.book)
        self.shelf.save()
        self.assertEqual(self.shelf.books.count(), 1)
        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            outgoing.handle_unshelve(self.local_user, self.book, self.shelf)
        self.assertEqual(self.shelf.books.count(), 0)

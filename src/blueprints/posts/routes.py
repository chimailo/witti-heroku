import random
from sqlalchemy import exc
from sqlalchemy.sql import func
from flask import url_for, request, jsonify, Blueprint, current_app

from src import db
from src.lib import urlsafe_base64
from src.lib.auth import authenticate
from src.blueprints.errors import server_error, bad_request, \
    not_found, error_response
from src.blueprints.messages.models import Notification
from src.blueprints.posts.models import Post
from src.blueprints.posts.schema import PostSchema


posts = Blueprint('posts', __name__, url_prefix='/api')


@posts.route('/posts/ping', methods=['GET'])
def ping():
    return {'message': 'Post Route!'}


@posts.route('/posts/featured', methods=['GET'])
def get_featured_posts():
    try:
        res = Post.query.filter(Post.comment_id.is_(None)).all()
        posts = random.sample(res, k=5)
    except Exception:
        return server_error('Something went wrong, please try again.')

    return jsonify(PostSchema(
        many=True, only=('id', 'body', 'author.profile')).dump(posts))


@posts.route('/posts/<int:post_id>', methods=['GET'])
@authenticate
def get_post(user, post_id):
    try:
        post = Post.find_by_id(post_id)

        if not post:
            return not_found('Post not found')
    except Exception:
        return server_error('Something went wrong, please try again.')
    else:
        post_dict = PostSchema().dump(post)
        post_dict['isLiked'] = post.is_liked_by(user)

        if user.id != post.author.id:
            post_dict['author']['isFollowing'] = user.is_following(post.author)

        return jsonify(post_dict)


@posts.route('/posts/explore', methods=['GET'])
@authenticate
def get_posts(user):
    feed = request.args.get('feed')
    cursor = request.args.get('cursor')
    items_per_page = current_app.config['ITEMS_PER_PAGE']
    nextCursor = None
    query = ''

    try:
        sorted_posts = Post.get_by_reactions().subquery()
        top_posts = db.session.query(Post, sorted_posts.c.sequence).join(
            sorted_posts, sorted_posts.c.id == Post.id).order_by(
                sorted_posts.c.sequence.desc())
        latest_posts = Post.query.filter(
            Post.comment_id.is_(None)).order_by(Post.created_on.desc())
    except Exception as e:
        db.session.rollback()
        print(e)
        return server_error('An unexpected error occured, please try again.')

    if cursor == '0' and feed == 'latest':
        query = latest_posts.limit(items_per_page + 1).all()
    elif cursor == '0' and feed == 'top':
        query = top_posts.limit(items_per_page + 1).all()
    else:
        if feed == 'latest':
            cursor = urlsafe_base64(cursor, from_base64=True)
            query = latest_posts.filter(
                Post.created_on < cursor).limit(items_per_page + 1).all()
        else:
            cursor = urlsafe_base64(cursor, from_base64=True)
            query = top_posts.filter(sorted_posts.c.sequence < cursor).limit(
                items_per_page + 1).all()

    if len(query) > items_per_page:
        nextCursor = urlsafe_base64(
            query[items_per_page - 1].created_on.isoformat()) \
                if feed == 'latest' else urlsafe_base64(
            str(query[items_per_page - 1][1]))

    posts = [post.to_dict(user) for post in query[:items_per_page]] \
        if feed == 'latest' else \
            [post[0].to_dict(user) for post in query[:items_per_page]]

    return {
        'data': posts,
        'nextCursor': nextCursor
    }


@posts.route('/posts', methods=['GET'])
@authenticate
def posts_feed(user):
    latest = request.args.get('latest')
    top = request.args.get('top')
    cursor = request.args.get('cursor')
    items_per_page = current_app.config['ITEMS_PER_PAGE']
    nextCursor = None
    query = ''

    try:
        followed_posts = user.get_followed_posts().subquery()
        posts_reactions = Post.get_reactions().subquery()
        top_followed_posts = db.session.query(
            followed_posts, func.row_number().over(
                order_by=posts_reactions.c.reactions).label(
                    'sequence')).outerjoin(
                        posts_reactions, followed_posts.c.posts_id ==
                        posts_reactions.c.id).subquery()

        top_posts = db.session.query(Post, top_followed_posts.c.sequence).join(
            Post, top_followed_posts.c.posts_id == Post.id).order_by(
                    top_followed_posts.c.sequence.desc())

        latest_posts = db.session.query(Post, followed_posts.c.posts_id).join(
            Post, Post.id == followed_posts.c.posts_id).order_by(
                Post.created_on.desc())
    except Exception as e:
        db.session.rollback()
        print(e)
        return server_error('An unexpected error occured, please try again.')

    if cursor == '0' and latest:
        query = latest_posts.limit(items_per_page + 1).all()
    elif cursor == '0' and top:
        query = top_posts.limit(items_per_page + 1).all()
    else:
        if latest:
            cursor = urlsafe_base64(cursor, from_base64=True)
            query = latest_posts.filter(
                Post.created_on < cursor).limit(items_per_page + 1).all()
        else:
            cursor = urlsafe_base64(cursor, from_base64=True)
            query = top_posts.filter(
                top_followed_posts.c.sequence < cursor).limit(
                    items_per_page + 1).all()

    if len(query) > items_per_page:
        nextCursor = urlsafe_base64(
            query[items_per_page - 1][0].created_on.isoformat()) \
                if latest else urlsafe_base64(
            str(query[items_per_page - 1][1]))

    return {
        'data': [post[0].to_dict(user) for post in query[:items_per_page]],
        'nextCursor': nextCursor
    }


@posts.route('/posts/<int:post_id>/comments', methods=['POST'])
@posts.route('/posts', methods=['POST'])
@authenticate
def create_post(user, post_id=None):
    req_data = request.get_json()

    if not req_data:
        return bad_request("No request data provided")

    post = Post()
    post.body = req_data.get('post')
    post.user_id = user.id
    db.session.add(post)

    if post_id:
        post.comment_id = post_id
        parent = Post.find_by_id(post_id)
        db.session.add(user.add_notification(subject='comment',
            item_id=post.id, id=parent.author.id, post_id=parent.id))
    else:
        post_notifs = []
        for u in user.followers.all():
            post_notifs.append(user.add_notification(
                    subject='post', item_id=post.id, id=u.id, post_id=post.id))
        db.session.add_all(post_notifs)

    try:
        post.save()
    except (exc.IntegrityError, ValueError):
        db.session.rollback()
        return server_error('Something went wrong, please try again.')
    else:
        response = jsonify(post.to_dict(user))
        response.status_code = 201
        response.headers['Location'] = url_for(
            'posts.get_post', post_id=post.id)
        return response


@posts.route('/posts/<int:post_id>', methods=['DELETE'])
@authenticate
def delete_post(user, post_id):
    post = Post.find_by_id(post_id)

    if not post:
        return not_found('Post not found.')

    if post.user_id != user.id:
        return error_response(401, "You cannot delete someone else's post.")

    post_notif = Notification.find_by_attr(subject='post', item_id=post.id)
    comment_notif = Notification.find_by_attr(
        subject='comment', item_id=post.id)

    [db.session.delete(notif) for notif in post_notif] if post_notif \
        else db.session.delete(comment_notif)

    try:
        post.delete()
    except (exc.IntegrityError, ValueError):
        db.session.rollback()
        return server_error('Something went wrong, please try again.')
    else:
        return {'message': 'Post was successfuly deleted.'}


@posts.route('/posts/<int:post_id>/likes', methods=['POST'])
@authenticate
def update_like(user, post_id):
    post = Post.find_by_id(post_id)

    if not post:
        return not_found('Post not found')

    try:
        if post.is_liked_by(user):
            post.likes.remove(user)
            db.session.delete(
                Notification.find_by_attr(subject='like', item_id=post.id))
        else:
            post.likes.append(user)
            db.session.add(user.add_notification(
                'like', item_id=post.id, id=post.author.id, post_id=post.id))

        post.save()
    except (exc.IntegrityError, ValueError):
        db.session.rollback()
        return server_error('Something went wrong, please try again.')
    else:
        return jsonify(post.to_dict(user))


@posts.route('/posts/<int:post_id>/comments', methods=['GET'])
@authenticate
def get_post_comments(user, post_id):
    post = Post.find_by_id(post_id)

    if not post:
        return not_found('Post not found.')

    cursor = request.args.get('cursor')
    items_per_page = current_app.config['ITEMS_PER_PAGE']
    nextCursor = None
    query = ''

    if cursor == '0':
        query = post.comments.order_by(
            Post.created_on.desc()).limit(items_per_page + 1).all()
    else:
        cursor = urlsafe_base64(cursor, from_base64=True)
        query = post.comments.order_by(
            Post.created_on.desc()).filter(
                Post.created_on < cursor).limit(items_per_page + 1).all()

    if len(query) > items_per_page:
        nextCursor = urlsafe_base64(
            query[items_per_page - 1].created_on.isoformat())

    comments = []
    for c in query[:items_per_page]:
        comment = c.to_dict(user)
        comment['parent'] = PostSchema(
            only=('id', 'body', 'author',)).dump(c.parent)
        comments.append(comment)

    return {
        'data': comments,
        'nextCursor': nextCursor
    }

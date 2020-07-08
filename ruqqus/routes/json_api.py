import time
from flask import *
from sqlalchemy import *

from ruqqus.helpers.wrappers import *
from ruqqus.helpers.get import *

from ruqqus.__main__ import app, cache
from ruqqus.classes.boards import Board


@app.route("/api/v1/guild/<boardname>", methods=["GET"])
def guild_info(boardname):
    guild = get_guild(boardname)

    return jsonify(guild.json)


@app.route("/api/v1/user/<username>", methods=["GET"])
def user_info(username):

    user=get_user(username)
    return jsonify(user.json)

@app.route("/api/v1/post/<pid>", methods=["GET"])
@auth_desired
def post_info(v, pid):

    post=get_post(pid)

    if not post.is_public and post.board.is_private and not post.board.can_view(v):
        abort(403)
        
    return jsonify(post.json)

@app.route("/api/v1/post", methods=["POST"])
@auth_required
def post_info(v):

    npost=json.loads(request.body)
    
    if "title" not in npost:
        # fail 400
        abort(400)
    
    title = npost.get("title", "")
    if re.match('^\s*$', title):
        # error="Please enter a better title.",
        # fail 400
        abort(400)
    
    if len(title)>500:
        # fail 400
        # error="500 character limit for titles."
        abort(400)
              
    #sanitize title
    title=bleach.clean(title)
    
    if "url" not in npost and "body" not in npost:
        # fail 400
        # error="please include URL or body text"
        abort(400)
        
    url=npost.get("url", "")
    if len(url)>2048:
        # error="URLs cannot be over 2048 characters",
        # fail 400
        abort(400)
        
    parsed_url=urlparse(npost.get("url", ""))
    if not (parsed_url.scheme and parsed_url.netloc) and not npost.get("body", None):
        # fail 400
        # error="Please enter a URL or some text.",
        abort(400)
        
    #Force https for submitted urls
    if npost.get("url"):
        new_url=ParseResult(scheme="https",
                            netloc=parsed_url.netloc,
                            path=parsed_url.path,
                            params=parsed_url.params,
                            query=parsed_url.query,
                            fragment=parsed_url.fragment)
        url=urlunparse(new_url)
    else:
        url=""

    body=npost.get("body","")
    
    #catch too-long body
    if len(str(body))>10000:
        #error="10000 character limit for text body",
        # fail 400
        abort(400)
    
    board_name=npost.get("board","general")
    board_name=board_name.lstrip("+")
    board_name=board_name.rstrip()
    
    board=get_guild(board_name, graceful=True)
    if not board:
        board=get_guild('general')
            
    if board.is_banned:
        # fail 403
        abort(403)
    
    if board.has_ban(v):
        #error=f"You are exiled from +{board.name}.",
        # fail 403
        abort(403)
    
    if (board.restricted_posting or board.is_private) and not (board.can_submit(v)):
        # error=f"You are not an approved contributor for +{board.name}.",
        # fail 403
        abort(403)

    #check for duplicate
    dup = g.db.query(Submission).join(Submission.submission_aux).filter(
      Submission.author_id==v.id,
      Submission.is_deleted==False,
      Submission.board_id==board.id,
      SubmissionAux.title==title, 
      SubmissionAux.url==url,
      SubmissionAux.body==body
      ).first()

    if dup:
        return jsonify(dup.permalink)
    
    #check for domain specific rules
    parsed_url=urlparse(url)
    domain=parsed_url.netloc

    # check ban status
    domain_obj=get_domain(domain)
    if domain_obj:
        if not domain_obj.can_submit:
           # fail 403
           # error=BAN_REASONS[domain_obj.reason]
            abort(403)
            
        #check for embeds
        if domain_obj.embed_function:
            try:
                embed=eval(domain_obj.embed_function)(url)
            except:
                embed=""
        else:
            embed=""
    else:
        embed=""
    
    user_id=v.id
    user_name=v.username
                
    #now make new post
    with CustomRenderer() as renderer:
        body_md=renderer.render(mistletoe.Document(body))
    body_html = sanitize(body_md, linkgen=True)

    #check for embeddable video
    domain=parsed_url.netloc

    if url:
        repost = g.db.query(Submission).join(Submission.submission_aux).filter(
        SubmissionAux.url.ilike(url),
        Submission.board_id==board.id,
        Submission.is_deleted==False, 
        Submission.is_banned==False
        ).order_by(
        Submission.id.asc()
        ).first()
    else:
      repost=None

    #offensive
    for x in g.db.query(BadWord).all():
        if (body and x.check(body)) or x.check(title):
            is_offensive=True
            break
        else:
            is_offensive=False

    new_post=Submission(
        author_id=user_id,
        domain_ref=domain_obj.id if domain_obj else None,
        board_id=board.id,
        original_board_id=board.id,
        over_18=(bool(npost.get("over_18","")) or board.over_18),
        post_public=not board.is_private,
        repost_id=repost.id if repost else None,
        is_offensive=is_offensive
    )

    g.db.add(new_post)
    g.db.flush()

    new_post_aux=SubmissionAux(
        id=new_post.id,
        url=url,
        body=body,
        body_html=body_html,
        embed_url=embed,
        title=title
    )
    
    g.db.add(new_post_aux)
    g.db.flush()

    vote=Vote(user_id=user_id,
              vote_type=1,
              submission_id=new_post.id
              )
    g.db.add(vote)
    g.db.flush()

    g.db.commit()
    g.db.refresh(new_post)

    #spin off thumbnail generation and csam detection as  new threads
    if new_post.url:
        new_thread=threading.Thread(target=thumbnail_thread,
                                    args=(new_post.base36id,)
                                    )
        new_thread.start()
        csam_thread = threading.Thread(target=check_csam, args=(new_post,))
        csam_thread.start()

    #expire the relevant caches: front page new, board new
    #cache.delete_memoized(frontlist, sort="new")
    g.db.commit()
    cache.delete_memoized(Board.idlist, board, sort="new")

    #print(f"Content Event: @{new_post.author.username} post {new_post.base36id}")
        
    return jsonify(new_post.json)

@app.route("/api/v1/comment/<cid>", methods=["GET"])
@auth_desired
def comment_info(v, cid):

    comment=get_comment(cid)

    post=comment.post
    if not post.is_public and post.board.is_private and not post.board.can_view(v):
        abort(403)
        
    return jsonify(comment.json)

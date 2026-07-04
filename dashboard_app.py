@app.get('/api/profile')
async def api_profile(request: Request):
    """Get logged-in user's profile."""
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    nickname = user.get('username')
    avatar = user.get('avatar')
    bio = user.get('bio', '')
    is_admin = bool(user.get('is_admin', False))

    if user.get('auth_type') == 'email':
        try:
            conn = _db()
            row = conn.execute(
                "SELECT nickname, avatar, bio, is_admin FROM auth_users WHERE auth_id = ?",
                (int(user['discord_id']),)
            ).fetchone()
            conn.close()
            if row:
                nickname = row['nickname'] or nickname
                avatar = row['avatar'] or avatar
                bio = row['bio'] or bio
                is_admin = bool(row['is_admin'])
        except Exception as e:
            log.warning(f"[profile] lookup failed: {e}")

    return {'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio, 'is_admin': is_admin}

    game = get_stats(atype, uid)
    if not game:
        return _json({'items': []}, 404)

    raw_inventory = game.get('inventory')
    items = []

    if isinstance(raw_inventory, dict):
        for name, info in raw_inventory.items():
            if isinstance(info, dict):
                items.append({
                    'name': info.get('name', name),
                    'quantity': info.get('quantity', info.get('qty', 1)),
                    'rarity': info.get('rarity', 'common'),
                })
            else:
                items.append({'name': name, 'quantity': info, 'rarity': 'common'})
    elif isinstance(raw_inventory, list):
        for entry in raw_inventory:
            if isinstance(entry, dict):
                items.append({
                    'name': entry.get('name', 'Unknown Item'),
                    'quantity': entry.get('quantity', entry.get('qty', 1)),
                    'rarity': entry.get('rarity', 'common'),
                })
            else:
                items.append({'name': str(entry), 'quantity': 1, 'rarity': 'common'})

    return {'items': items}

@app.get('/api/auctions')
async def api_auctions():
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM auctions WHERE status='active' ORDER BY end_time ASC LIMIT 50"
        ).fetchall()
        conn.close()

        auctions = []
        for r in rows:
            try:
                tl = max(0, int((datetime.fromisoformat(r['end_time']) - datetime.now(UTC)).total_seconds()))
            except Exception:
                tl = 0

            auctions.append({
                'id': r['id'],
                'name': r['item_name'],
                'icon': '⚔️',
                'rarity': 'common',
                'category': 'weapon',
                'seller': str(r['seller_id']),
                'current_bid': r['current_bid'],
                'bids': 0,
                'time_left': tl,
                'top_bidder': str(r['highest_bidder']) if r['highest_bidder'] else None,
                'bid_history': [],
            })
        return {'auctions': auctions}
    except Exception as e:
        log.warning(f'[api/auctions] {e}')
        return _json({'auctions': []}, 200)

@app.get('/api/profile')
async def api_profile(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    stats = get_stats(user.get('auth_type', 'discord'), user['discord_id'])
    nickname = stats.get('name') or user.get('username')
    avatar = stats.get('avatar') or user.get('avatar')
    bio = stats.get('bio', '')
    is_admin = _is_admin_email(user.get('email', ''))

    return {'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio,
            'is_admin': is_admin, 'stats': stats, **{k: stats.get(k) for k in
            ('level', 'gold', 'gems', 'special_coins', 'xp', 'xp_needed', 'char_class')}}

def run_dashboard():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 25507))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, 0))
                free_port = s.getsockname()[1]
            log.warning(f"[Dashboard] Port {port} in use; switching to {free_port}")
            port = free_port
        except Exception as e:
            log.error(f"[Dashboard] Failed to find free port: {e}")
            raise SystemExit(1)

    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == '__main__':
    run_dashboard()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import billing

def cmd_pending():
    orders = billing.list_orders(50)
    pending = [o for o in orders if o.get('status') == 'pending']
    if not pending:
        print('NO_PENDING')
        return
    for o in pending:
        print(f"{o['id']}|{o['merchant_trade_no']}|{o['username']}|{o['plan']}|{o['amount_text']}|{o['created_local']}")

def cmd_approve(key: str, admin: str = 'Telegram'):
    key = str(key).strip()
    orders = billing.list_orders(100)
    target_trade = None
    
    # 1. Match by merchant_trade_no
    for o in orders:
        if o.get('merchant_trade_no') == key:
            target_trade = key
            break
            
    # 2. Match by id or pay_code
    if not target_trade:
        for o in orders:
            if str(o.get('id')) == key or billing.pay_code(o.get('id')) == key:
                target_trade = o.get('merchant_trade_no')
                break
                
    # 3. Match latest pending by username
    if not target_trade:
        for o in orders:
            if o.get('username', '').casefold() == key.casefold() and o.get('status') == 'pending':
                target_trade = o.get('merchant_trade_no')
                break

    if not target_trade:
        print(f'NOT_FOUND:{key}')
        return

    order = billing.mark_received(target_trade, admin)
    if order and order.get('status') == 'paid':
        sub = billing.get_subscription(order.get('username'))
        exp = sub.get('expires_at')[:10] if sub else ''
        print(f"SUCCESS|{order.get('id')}|{order.get('merchant_trade_no')}|{order.get('username')}|{order.get('plan')}|{order.get('amount')}|{exp}")
    else:
        print(f'FAILED:{target_trade}')

def cmd_set_vip(username: str, days: int):
    res = billing.set_subscription_days(username, days)
    print(f"VIP_SUCCESS|{username}|{days}|{res.get('expires_at')[:10]}")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('USAGE: admin_cli.py [pending|approve <key>|vip <username> <days>]')
        sys.exit(1)
    action = sys.argv[1]
    if action == 'pending':
        cmd_pending()
    elif action == 'approve':
        if len(sys.argv) < 3:
            print('MISSING_KEY')
        else:
            cmd_approve(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 'Telegram')
    elif action == 'vip':
        if len(sys.argv) < 4:
            print('MISSING_ARGS')
        else:
            cmd_set_vip(sys.argv[2], int(sys.argv[3]))

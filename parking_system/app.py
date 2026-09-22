from flask import Flask, request, render_template, session, redirect, url_for ,jsonify,send_file, render_template_string, send_file
from datetime import datetime , timedelta
from bson.objectid import ObjectId
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.parser import parse as dtparse
import io
import random
import csv
import string
import atexit
atexit.register(lambda: scheduler.shutdown())


app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key'  # Change this in production!
app.config["MONGO_URI"] = "mongodb+srv://ags_2024:Aravind2024@cluster0.liwegzs.mongodb.net/parking_system?retryWrites=true&w=majority&appName=Cluster0"

mongo = PyMongo(app)

from flask_mail import Mail, Message

# After your Flask() app setup:
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'cherishchandrareddy150@gmail.com'  # your admin/support email
app.config['MAIL_PASSWORD'] = 'emni lvlb nacv bqnh'  # use Gmail app password, NOT your main password
app.config['MAIL_DEFAULT_SENDER'] = app.config['MAIL_USERNAME']

mail = Mail(app)


def generate_userid():
    return ''.join(random.choices(string.digits, k=6))


from dateutil.parser import parse as dtparse

BUFFER_MINUTES = 3

def annotate_buffer(slots):
    """Mark slots as 'buffer' if still in buffer period."""
    now_dt = datetime.utcnow()
    for slot in slots:
        buf = slot.get('buffer_until')
        if slot['status'] == 'available' and buf:
            if isinstance(buf, str):
                try: buf_dt = dtparse(buf)
                except Exception: continue
            else:
                buf_dt = buf
            if now_dt < buf_dt:
                slot['status'] = 'buffer'
                slot['buffer_remaining'] = int((buf_dt - now_dt).total_seconds() // 60)
    return slots



# --- NOTIFICATION REMINDER UTILITY ---
def send_booking_reminder(user_id, booking):
    """Create a notification for the user if not already sent for this booking."""
    already_notified = mongo.db.notifications.find_one({
        "user_id": user_id,
        "booking_id": str(booking["_id"]),
        "type": "reminder"
    })
    if not already_notified:
        mongo.db.notifications.insert_one({
            "user_id": user_id,
            "booking_id": str(booking["_id"]),
            "type": "reminder",
            "message": f"⏰ Reminder: Your booking for slot {booking['slot_id']} ends at {booking['end_time']}",
            "created_at": datetime.utcnow()
        })

# --- USER ROUTES ---

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form["phone"].strip()
        password = request.form["password"]
        user = mongo.db.users.find_one({"phone": phone})
        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["user_id"]
            session["role"] = "user"
            return redirect(url_for("user_dashboard"))
        else:
            return render_template("login.html", error="Invalid phone or password.")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        phone = ''.join(filter(str.isdigit, request.form["phone"]))
        password = request.form["password"]
        email = request.form['email'].strip()
        car_type = request.form["car_type"]
        car_number = request.form["car_number"].strip().upper()
        if len(phone) != 10:
            return render_template("register.html", error="Phone must be 10 digits.")
        if mongo.db.users.find_one({"phone": phone}):
            return render_template("register.html", error="Phone already registered.")
        user_id = generate_userid()
        hashed_pw = generate_password_hash(password)
        mongo.db.users.insert_one({
            "user_id": user_id,
            "phone": phone,
            "email": email,
            "password": hashed_pw,
            "car_type": car_type,
            "car_number": car_number,
            "wallet_balance": 0
        })
        session.clear()
        session["user_id"] = user_id
        session["role"] = "user"
        return redirect(url_for("user_dashboard"))
    return render_template("register.html")

@app.route('/dashboard')
def user_dashboard():
    if "user_id" not in session or session.get("role") != "user":
        return redirect(url_for("login"))
    user = mongo.db.users.find_one({"user_id": session["user_id"]})
    active_booking = mongo.db.bookings.find_one({
        "user_id": user["user_id"],
        "status": "active"
    })
    # Reminder: send reminder if booking ends in ≤15 min and hasn't been sent
    if active_booking:
        try:
            end_dt = datetime.fromisoformat(active_booking['end_time'])
        except Exception:
            end_dt = None
        now = datetime.utcnow()
        if end_dt:
            time_left = (end_dt - now).total_seconds() / 60
            if 0 < time_left <= 15:
                send_booking_reminder(user["user_id"], active_booking)
    notifications = []
    try:
        notes_cursor = mongo.db.notifications.find({"user_id": user["user_id"]}).sort('created_at', -1).limit(5)
        notifications = [note['message'] for note in notes_cursor]
    except Exception:
        notifications = []
    return render_template("user_dashboard.html", user=user, active_booking=active_booking, notifications=notifications)

from dateutil.parser import parse as dtparse  # Make sure this is at the top of your app.py

@app.route('/booking', methods=['GET', 'POST'])
def booking():
    if 'user_id' not in session or session.get("role") != "user":
        return redirect(url_for('login'))
    user_id = session['user_id']
    message = None

    slot_type = request.args.get('slot_type')
    filter_query = {}
    if slot_type in ['EV', 'Fuel']:
        filter_query['type'] = slot_type

    slots = list(mongo.db.slots.find(filter_query))
    # No annotate_buffer, just direct list
    rows = [slots[i:i+10] for i in range(0, len(slots), 10)]
    active_booking = mongo.db.bookings.find_one({'user_id': user_id, 'status': 'active'})
    default_policy = {'rate_ev': 60, 'rate_fuel': 50, 'fee_low': 10, 'fee_high': 50}
    policy = mongo.db.policy.find_one() or default_policy

    if request.method == 'POST':
        slot_id = request.form.get('slot_id')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        slot = mongo.db.slots.find_one({'slot_id': slot_id})
        if active_booking:
            message = "You already have an active booking. Cancel it first."
        elif not slot_id or not start_time or not end_time:
            message = "Please select a slot and valid start/end times."
        elif not slot or slot.get('status') != 'available':
            message = "The selected slot is not available."
        else:
            try:
                start_dt = datetime.fromisoformat(start_time)
                end_dt = datetime.fromisoformat(end_time)
            except Exception:
                message = "Invalid start/end times."
                return render_template('booking.html', lot_rows=rows, active_booking=active_booking, message=message,
                                      now=datetime.now().isoformat(timespec='minutes'), policy=policy,
                                      slot_type=slot_type)
            if end_dt <= start_dt:
                message = "End time must be after start time."
            else:
                hours = max(1, int((end_dt - start_dt).total_seconds() // 3600))
                rate = policy['rate_ev'] if slot['type'] == 'EV' else policy['rate_fuel']
                amount = hours * rate
                user = mongo.db.users.find_one({'user_id': user_id})
                if user['wallet_balance'] < amount:
                    message = f"Insufficient wallet balance for booking: ₹{amount} needed."
                else:
                    # Mark as booked & update user
                    mongo.db.users.update_one({'user_id': user_id}, {'$inc': {'wallet_balance': -amount}})
                    mongo.db.slots.update_one({'slot_id': slot_id}, {'$set': {'status': 'booked'}})
                    booking_doc = {
                        'user_id': user_id,
                        'slot_id': slot_id,
                        'slot_type': slot['type'],
                        'start_time': start_time,
                        'end_time': end_time,
                        'reminder_sent': False,
                        'amount': amount,
                        'status': 'active',
                        'created_at': datetime.utcnow()
                    }
                    inserted = mongo.db.bookings.insert_one(booking_doc)
                    message = f"Slot {slot_id} booked for {hours} hour(s)! ₹{amount} deducted."
                    active_booking = mongo.db.bookings.find_one({'_id': inserted.inserted_id})
                    slots = list(mongo.db.slots.find(filter_query))
                    rows = [slots[i:i+10] for i in range(0, len(slots), 10)]

    now_str = datetime.now().isoformat(timespec='minutes')
    return render_template(
        'booking.html',
        lot_rows=rows,
        active_booking=active_booking,
        message=message,
        now=now_str,
        policy=policy,
        slot_type=slot_type
    )


@app.route('/cancel_booking/<booking_id>', methods=['POST'])
def cancel_booking(booking_id):
    if 'user_id' not in session or session.get("role") != "user":
        return redirect(url_for('login'))
    booking = mongo.db.bookings.find_one({'_id': ObjectId(booking_id)})
    if not booking or booking['user_id'] != session['user_id'] or booking['status'] != 'active':
        return redirect(url_for('booking'))
    policy = mongo.db.policy.find_one() or {'rate_ev':60, 'rate_fuel':50, 'fee_low':10, 'fee_high':50}
    start_dt = datetime.fromisoformat(booking['start_time'])
    now_dt = datetime.utcnow()
    hour_diff = (start_dt - now_dt).total_seconds() / 3600.0
    fee_percent = policy['fee_low'] if hour_diff >= 3 else policy['fee_high']
    refund_amt = int(booking['amount'] * (1 - fee_percent / 100.0))
    buffer_until = datetime.utcnow() + timedelta(minutes=BUFFER_MINUTES)
    mongo.db.bookings.update_one({'_id': ObjectId(booking_id)}, {'$set': {'status': 'cancelled','refund_amt': refund_amt}})
    mongo.db.slots.update_one({'slot_id': booking['slot_id']}, {
    '$set': {'status': 'available'}
    })

    mongo.db.users.update_one({'user_id': booking['user_id']}, {'$inc': {'wallet_balance': refund_amt}})
    return redirect(url_for('booking'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- ADMIN ROUTES ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        admin = mongo.db.admins.find_one({'username': username})
        if admin and admin['password'] == password:

            session.clear()
            session['admin_id'] = str(admin['_id'])
            session['role'] = "admin"
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template("admin_login.html", error='Invalid credentials.')
    return render_template("admin_login.html")

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))
    stats = {
        'total_slots': mongo.db.slots.count_documents({}),
        'booked_slots': mongo.db.slots.count_documents({'status': 'booked'}),
        'ev_available': mongo.db.slots.count_documents({'type': 'EV', 'status': 'available'}),
        'fuel_available': mongo.db.slots.count_documents({'type': 'Fuel', 'status': 'available'})
    }
    return render_template('admin_dashboard.html', stats=stats)

@app.route('/admin/slots', methods=['GET', 'POST'])
def manage_slots():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))
    msg, error = None, None
    if request.method == 'POST':
        slot_id = request.form.get('slot_id', '').strip()
        if "add_slot" in request.form:
            slot_type = request.form.get('type', 'Fuel')
            slot_status = request.form.get('status', 'available')
            if mongo.db.slots.find_one({'slot_id': slot_id}):
                error = "Slot ID already exists."
            else:
                mongo.db.slots.insert_one({
                    'slot_id': slot_id,
                    'type': slot_type,
                    'status': slot_status
                })
                msg = f"Slot {slot_id} added."
        elif "toggle_status" in request.form:
            slot = mongo.db.slots.find_one({'slot_id': slot_id})
            if slot:
                if slot['status'] == 'available':
                    mongo.db.slots.update_one({'slot_id': slot_id}, {'$set': {'status': 'booked'}})
                    msg = f"Slot {slot_id} marked as booked."
                else:
                    mongo.db.slots.update_one({'slot_id': slot_id}, {'$set': {'status': 'available'}})
                    msg = f"Slot {slot_id} activated."
        elif "set_maintenance" in request.form:
            mongo.db.slots.update_one({'slot_id': slot_id}, {'$set': {'status': 'maintenance'}})
            msg = f"Slot {slot_id} set to maintenance."
        elif "delete_slot" in request.form:
            mongo.db.slots.delete_one({'slot_id': slot_id})
            msg = f"Slot {slot_id} deleted."
    slots = list(mongo.db.slots.find())
    slot_grid = [slots[i:i+10] for i in range(0, len(slots), 10)]
    return render_template(
        'slot_management.html',
        slots=slots,
        slot_grid=slot_grid,
        msg=msg,
        error=error
    )

@app.route('/admin/wallet_approvals', methods=['GET','POST'])
def wallet_approvals():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))
    msg = None
    if request.method == 'POST':
        pid = request.form['payment_id']
        action = request.form['action']
        admin_remark = request.form.get('admin_remark', "")
        payment = mongo.db.payments.find_one({'_id': ObjectId(pid)})
        if payment and payment['status'] == "pending":
            if action == "approve":
                mongo.db.users.update_one({'user_id':payment['user_id']},{'$inc': {'wallet_balance':payment['amount']}})
                mongo.db.payments.update_one({'_id': ObjectId(pid)}, {'$set': {'status': 'approved', 'approved_at': datetime.utcnow()}})
                msg = f"Payment approved and wallet recharged for user {payment['user_id']}."
            elif action == "reject":
                mongo.db.payments.update_one({'_id': ObjectId(pid)}, {'$set': {'status': 'rejected', 'admin_remark': admin_remark, 'reviewed_at': datetime.utcnow()}})
                msg = f"Payment request rejected."
    payments = list(mongo.db.payments.find().sort('requested_at', -1))
    return render_template('admin_wallet.html', payments=payments, msg=msg)

@app.route('/admin/pricing', methods=['GET','POST'])
def pricing():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))
    msg = None
    policy = mongo.db.policy.find_one() or {'rate_ev':60, 'rate_fuel':50, 'fee_low':10, 'fee_high':50}
    if request.method == 'POST':
        try:
            rate_ev = int(request.form['rate_ev'])
            rate_fuel = int(request.form['rate_fuel'])
            fee_low = int(request.form['fee_low'])
            fee_high = int(request.form['fee_high'])
            policy = {
                'rate_ev': rate_ev,
                'rate_fuel': rate_fuel,
                'fee_low': fee_low,
                'fee_high': fee_high
            }
            mongo.db.policy.update_one({}, {'$set': policy}, upsert=True)
            msg = "Policy updated successfully."
        except Exception:
            msg = "Invalid input."
        policy = mongo.db.policy.find_one()
    return render_template('pricing.html', policy=policy, msg=msg)

@app.route('/admin/notifications', methods=['GET', 'POST'])
def admin_notifications():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))

    notif_type = request.args.get('type')
    filter_q = {}
    if notif_type and notif_type != "all":
        filter_q['type'] = notif_type

    # Mark as read or delete
    if request.method == "POST":
        action = request.form.get("action")
        notif_id = request.form.get("notif_id")
        if action == "read":
            mongo.db.notifications.update_one({"_id": ObjectId(notif_id)}, {"$set": {"read": True}})
        elif action == "delete":
            mongo.db.notifications.delete_one({"_id": ObjectId(notif_id)})
        elif action == "clearall":
            mongo.db.notifications.delete_many({"type": notif_type} if notif_type and notif_type != "all" else {})
        return redirect(url_for('admin_notifications', type=notif_type if notif_type else 'all'))

    notes = list(mongo.db.notifications.find(filter_q).sort("created_at", -1))
    type_list = ["all", "reminder", "maintenance", "booking"]
    return render_template('admin_notifications.html', notes=notes, notif_type=notif_type or "all", type_list=type_list)



@app.route('/receipt_pdf/<booking_id>')
def receipt_pdf(booking_id):
    if 'user_id' not in session or session.get("role") != "user":
        return redirect(url_for('login'))
    booking = mongo.db.bookings.find_one({'_id': ObjectId(booking_id), 'user_id': session['user_id']})
    if not booking:
        return "Receipt not found or access denied.", 404
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 750, "PARKiT Booking Receipt")
    c.setFont("Helvetica", 11)
    lines = [
        f"Slot: {booking['slot_id']} ({booking['slot_type']})",
        f"From: {booking['start_time']}",
        f"To: {booking['end_time']}",
        f"Status: {booking['status'].capitalize()}",
        f"Amount Paid: ₹{booking['amount']}",
        f"Refunded: ₹{booking.get('refund_amt', 0) if booking['status']=='cancelled' else '--'}",
        f"Booking Date: {booking['created_at'].strftime('%Y-%m-%d %H:%M') if booking.get('created_at') else ''}",
        f"Booking ID: {booking['_id']}",
        "Thank you for using PARKiT!"
    ]
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=f"Parkit_Receipt_{booking_id}.pdf")


@app.route('/admin/analytics', methods=['GET'])
def admin_analytics():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))

    date_format = "%Y-%m-%d"
    today = datetime.utcnow().date()
    start = request.args.get('start')
    end = request.args.get('end')
    try:
        start_date = datetime.strptime(start, date_format).date() if start else today - timedelta(days=6)
        end_date = datetime.strptime(end, date_format).date() if end else today
    except Exception:
        start_date = today - timedelta(days=6)
        end_date = today

    # Build date range list
    date_list = [(start_date + timedelta(days=i)) for i in range((end_date - start_date).days + 1)]

    # Only include bookings that start in the date window
    bookings = list(mongo.db.bookings.find({
        'created_at': {'$gte': datetime.combine(start_date, datetime.min.time()), '$lte': datetime.combine(end_date, datetime.max.time())}
    }))

    # Pie chart stats (status count)
    status_count = {"active":0, "cancelled":0, "completed":0}
    slot_type_count = {"EV":0, "Fuel":0}
    for b in bookings:
        if b.get("status") in status_count: status_count[b['status']] += 1
        slot_type_count[b.get("slot_type", "Fuel")] += 1

    # Trends: bookings per day
    daily_counts = {d.strftime(date_format):0 for d in date_list}
    for b in bookings:
        if isinstance(b.get("created_at"), datetime):
            dkey = b['created_at'].strftime(date_format)
            if dkey in daily_counts:
                daily_counts[dkey] += 1

    return render_template(
        "admin_analytics.html",
        date_list=[d.strftime(date_format) for d in date_list],
        daily_counts=[daily_counts[d.strftime(date_format)] for d in date_list],
        status_count=status_count,
        slot_type_count=slot_type_count,
        start=start_date.strftime(date_format),
        end=end_date.strftime(date_format),
        booking_csv_url=url_for('export_analytics_csv', start=start_date.strftime(date_format), end=end_date.strftime(date_format))
    )

@app.route('/admin/export_analytics_csv')
def export_analytics_csv():
    if 'admin_id' not in session or session.get("role") != "admin":
        return "Not authorized", 403
    date_format = "%Y-%m-%d"
    today = datetime.utcnow().date()
    start = request.args.get('start')
    end = request.args.get('end')
    try:
        start_date = datetime.strptime(start, date_format).date() if start else today - timedelta(days=6)
        end_date = datetime.strptime(end, date_format).date() if end else today
    except Exception:
        start_date = today - timedelta(days=6)
        end_date = today

    bookings = list(mongo.db.bookings.find({
        'created_at': {'$gte': datetime.combine(start_date, datetime.min.time()), '$lte': datetime.combine(end_date, datetime.max.time())}
    }))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Booking ID','User','Slot ID','Slot Type','Start','End','Amount','Status','Refund','Booked At'])
    for b in bookings:
        writer.writerow([
            str(b.get('_id', '')),
            b.get('user_id',''),
            b.get('slot_id',''),
            b.get('slot_type',''),
            b.get('start_time',''),
            b.get('end_time',''),
            b.get('amount',''),
            b.get('status',''),
            b.get('refund_amt',''),
            b.get('created_at').strftime("%Y-%m-%d %H:%M") if b.get('created_at') else ''
        ])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()), mimetype="text/csv", as_attachment=True, download_name="parkit_analytics.csv")
# --- Utility/User Views ---


@app.route('/wallet', methods=['GET', 'POST'])
def wallet():
    if 'user_id' not in session or session.get("role") != "user":
        return redirect(url_for('login'))
    user = mongo.db.users.find_one({'user_id': session['user_id']})
    msg = None
    pending_payment = mongo.db.payments.find_one({'user_id': user['user_id'], 'status': {'$in': ['pending', 'rejected']}}, sort=[('_id', -1)])
    if request.method == 'POST':
        if pending_payment and pending_payment['status'] == "pending":
            msg = "You already have a pending payment awaiting admin approval."
        else:
            try:
                amount = int(request.form['amount'])
                method = request.form['payment_method']
                payment_id = request.form.get('payment_id',"").strip()
                if amount < 1 or amount > 10000 or method not in ["UPI","Card","NetBanking"]:
                    msg = "Please enter a valid amount and payment method."
                elif (method == "UPI" and not payment_id) or (method in ["Card","NetBanking"] and not payment_id):
                    msg = "Please enter a valid Transaction/Payment ID."
                else:
                    mongo.db.payments.insert_one({
                        'user_id': user['user_id'],
                        'amount': amount,
                        'method': method,
                        'payment_id': payment_id,
                        'status': "pending",
                        'requested_at': datetime.utcnow()
                    })
                    msg = "Payment request submitted for admin approval. Watch here for status."
            except Exception:
                msg = "Invalid input."
            user = mongo.db.users.find_one({'user_id': session['user_id']})
            pending_payment = mongo.db.payments.find_one({'user_id': user['user_id'], 'status': {'$in': ['pending', 'rejected']}}, sort=[('_id', -1)])
    return render_template('wallet.html', user=user, msg=msg, pending_payment=pending_payment)

@app.route('/notifications')
def notifications():
    return "Notifications page (To implement)"

@app.route('/chatbot')
def chatbot():
    if "user_id" not in session or session.get("role") != "user":
        return redirect(url_for("login"))
    return render_template("chatbot.html")

from flask import jsonify

from flask import jsonify, session, request
from datetime import datetime

from flask import jsonify, session, request
from datetime import datetime, timedelta

@app.route('/chatbot_api', methods=['POST'])
def chatbot_api():
    if 'user_id' not in session:
        return jsonify({'reply': "Please log in to use PARKiT Assistant."})
    user_id = session['user_id']
    user = mongo.db.users.find_one({'user_id': user_id})
    msg = request.json.get('message', '').strip().lower()

    chat = mongo.db.chat_sessions.find_one({'user_id': user_id}) or {}

    # --- Multi-turn Booking Conversation ---
    if chat.get('intent') == 'booking':
        step = chat.get('booking_step', 1)
        if step == 1:
            if "ev" in msg:
                chat['data'] = {'type': "EV"}
            elif "fuel" in msg:
                chat['data'] = {'type': "Fuel"}
            else:
                reply = "Is your preferred slot type EV or Fuel?"
                chat['intent'], chat['booking_step'] = 'booking', 1
                mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': chat}, upsert=True)
                return jsonify({'reply': reply})

            reply = "Great! What start date and time do you want? (e.g. 2025-08-24 15:00)"
            chat['intent'], chat['booking_step'] = 'booking', 2
            mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': chat}, upsert=True)
            return jsonify({'reply': reply})

        elif step == 2:
            try:
                start_dt = datetime.strptime(msg, "%Y-%m-%d %H:%M")
                chat['data']['start_time'] = start_dt.strftime("%Y-%m-%dT%H:%M")
                reply = "And what end date and time? (e.g. 2025-08-24 18:00)"
                chat['intent'], chat['booking_step'] = 'booking', 3
                mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': chat}, upsert=True)
                return jsonify({'reply': reply})
            except:
                reply = "Please enter start date and time as YYYY-MM-DD HH:MM."
                mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': chat}, upsert=True)
                return jsonify({'reply': reply})

        elif step == 3:
            try:
                end_dt = datetime.strptime(msg, "%Y-%m-%d %H:%M")
                chat['data']['end_time'] = end_dt.strftime("%Y-%m-%dT%H:%M")
                slot = mongo.db.slots.find_one({'type':chat['data']['type'], 'status':'available'})
                if slot:
                    rate = (mongo.db.policy.find_one() or {'rate_ev':60, 'rate_fuel':50})
                    amt = rate['rate_ev'] if slot['type']=='EV' else rate['rate_fuel']
                    sdt = datetime.fromisoformat(chat['data']['start_time'])
                    edt = datetime.fromisoformat(chat['data']['end_time'])
                    hours = max(1, int((edt-sdt).total_seconds()//3600))
                    total_amt = amt * hours
                    if user['wallet_balance'] < total_amt:
                        reply = f"Insufficient balance: need ₹{total_amt}, your wallet: ₹{user['wallet_balance']}."
                        mongo.db.chat_sessions.delete_one({'user_id': user_id})
                        return jsonify({'reply': reply})
                    mongo.db.users.update_one({'user_id': user_id},{'$inc': {'wallet_balance': -total_amt}})
                    mongo.db.slots.update_one({'slot_id': slot['slot_id']}, {'$set': {'status':'booked', 'buffer_until': None}})
                    booking_doc = {
                        'user_id': user_id,
                        'slot_id': slot['slot_id'],
                        'slot_type': slot['type'],
                        "reminder_sent": False,
                        'start_time': chat['data']['start_time'],
                        'end_time': chat['data']['end_time'],
                        'amount': total_amt,
                        'status': 'active',
                        'created_at': datetime.utcnow()
                    }
                    mongo.db.bookings.insert_one(booking_doc)
                    reply = f"Slot {slot['slot_id']} ({slot['type']}) booked for you, {hours} hour(s)! ₹{total_amt} deducted.\nIs there anything else I can help you with?"
                    mongo.db.chat_sessions.delete_one({'user_id': user_id})
                    return jsonify({'reply': reply})
                else:
                    reply = "Sorry, no available slots of this type for this time. Try changing slot type or time."
                    mongo.db.chat_sessions.delete_one({'user_id': user_id})
                    return jsonify({'reply': reply})
            except:
                reply = "Please enter end date and time as YYYY-MM-DD HH:MM."
                mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': chat}, upsert=True)
                return jsonify({'reply': reply})

    # --- Start Booking Intent ---
    if 'book' in msg or 'reserve' in msg:
        reply = "To book a slot, do you want EV or Fuel type?"
        mongo.db.chat_sessions.update_one({'user_id': user_id}, {'$set': {
            'user_id': user_id, 'intent': 'booking', 'booking_step': 1, 'data': {}
        }}, upsert=True)
        return jsonify({'reply': reply})

    # --- Smarter Smalltalk & Acknowledgement Handler ---
    if msg in ("ok", "okay", "great", "good", "yes", "fine", "awesome", "cool", "thank you", "thanks", "nice", "alright"):
        reply = "You're welcome! 🚗 If you need something else, just ask or type 'help'."
        return jsonify({'reply': reply})

    # --- Standard shortcuts as before ---
    if msg in ("hi", "hello", "hey", "good morning", "good afternoon", "good evening"):
        reply = "Hello! 👋 How can I assist you today? Type 'help' to see what I can do."
    elif "wallet" in msg:
        reply = f"Your current wallet balance is ₹{user['wallet_balance']}."
    elif "available slot" in msg or "slots available" in msg:
        available = list(mongo.db.slots.find({'status': 'available'}))
        count = len(available)
        ev = sum(1 for s in available if s['type']=='EV')
        fuel = sum(1 for s in available if s['type']=='Fuel')
        reply = f"There are {count} slots available: {ev} EV and {fuel} Fuel."
    elif "my booking" in msg or "my current slot" in msg:
        booking = mongo.db.bookings.find_one({'user_id':user_id,'status':'active'})
        if booking:
            reply = f"Your active booking: Slot {booking['slot_id']} ({booking['slot_type']}) from {booking['start_time']} to {booking['end_time']}. Amount: ₹{booking['amount']}."
        else:
            reply = "You have no active booking."
    elif "cancel my booking" in msg and "confirm" in msg:
        booking = mongo.db.bookings.find_one({'user_id':user_id,'status':'active'})
        if booking:
            policy = mongo.db.policy.find_one() or {'fee_low':10, 'fee_high':50}
            start_dt = datetime.fromisoformat(booking['start_time'])
            now_dt = datetime.utcnow()
            hour_diff = (start_dt - now_dt).total_seconds() / 3600.0
            fee_percent = policy['fee_low'] if hour_diff >= 3 else policy['fee_high']
            refund_amt = int(booking['amount'] * (1 - fee_percent / 100.0))
            buffer_until = datetime.utcnow() + timedelta(minutes=30)
            mongo.db.bookings.update_one({'_id': booking['_id']},{'$set': {'status':'cancelled','refund_amt':refund_amt}})
            mongo.db.slots.update_one({'slot_id': booking['slot_id']},{'$set':{'status':'available','buffer_until':buffer_until}})
            mongo.db.users.update_one({'user_id':user_id},{'$inc':{'wallet_balance':refund_amt}})
            reply = f"Your booking is cancelled. Refund of ₹{refund_amt} processed (fee applied)."
        else:
            reply = "You have no active booking to cancel."
    elif "help" in msg or "what can you do" in msg:
        reply = "I can show wallet balance, available slots, your booking, and assist with booking or cancellation. You can say 'book a slot' to start!"
    else:
        reply = "Sorry, I didn't understand. Try typing 'book a slot', 'help', or another PARKiT request."
    return jsonify({'reply': reply})

@app.route('/booking_history')
def booking_history():
    if 'user_id' not in session or session.get("role") != "user":
        return redirect(url_for('login'))
    bookings = list(mongo.db.bookings.find({'user_id': session['user_id']}))
    bookings.sort(key=lambda b: b.get('start_time', ''), reverse=True)
    return render_template('booking_history.html', bookings=bookings)


from flask import redirect

@app.route('/get_directions')
def get_directions():
    # Use fixed coordinates
    lat = 12.985862749188732
    lon = 77.76209660951397

    gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
    return redirect(gmaps_url)



@app.route('/admin/manage_lots', methods=['GET', 'POST'])
def manage_lots():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))
    msg = None
    lot = mongo.db.lot_locations.find_one({'lot_id': "main"})
    if request.method == 'POST':
        lot_name = request.form.get('name', '').strip()
        try:
            lat = float(request.form.get('latitude'))
            lon = float(request.form.get('longitude'))
            assert -90 <= lat <= 90 and -180 <= lon <= 180
        except (ValueError, AssertionError):
            msg = "Invalid latitude/longitude."
        else:
            mongo.db.lot_locations.update_one({'lot_id': 'main'},
                {'$set': {'name': lot_name, 'latitude': lat, 'longitude': lon, 'lot_id': "main"}}, upsert=True)
            msg = "Location updated!"
            lot = mongo.db.lot_locations.find_one({'lot_id': "main"})
    return render_template("manage_lots.html", lot=lot, msg=msg)


@app.route('/admin/manage_caretakers', methods=['GET', 'POST'])
def manage_caretakers():
    if 'admin_id' not in session or session.get("role") != "admin":
        return redirect(url_for('admin_login'))

    msg = err = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == "add":
            name = request.form['name'].strip()
            phone = request.form['phone'].strip()
            email = request.form['email'].strip()
            slots = [s.strip() for s in request.form.get('assigned_slots','').split(',') if s.strip()]
            password = request.form['password'].strip()
            hashed_pw = generate_password_hash(password)
            mongo.db.caretakers.insert_one({
                "name": name, "phone": phone, "email": email,
                "password": hashed_pw,
                "assigned_slots": slots, "status": "active"
            })
            msg = "Caretaker added!"
        elif action == "update":
            cid = request.form['caretaker_id']
            name = request.form['name'].strip()
            phone = request.form['phone'].strip()
            email = request.form['email'].strip()
            slots = [s.strip() for s in request.form.get('assigned_slots','').split(',') if s.strip()]
            status = request.form.get('status','active')
            mongo.db.caretakers.update_one({'_id': ObjectId(cid)},
                {'$set':{"name":name, "phone":phone, "email":email, "assigned_slots":slots, "status":status}})
            msg = "Caretaker updated."
        elif action == "delete":
            cid = request.form['caretaker_id']
            mongo.db.caretakers.delete_one({'_id': ObjectId(cid)})
            msg = "Caretaker deleted."

    caretakers = list(mongo.db.caretakers.find().sort("name", 1))
    return render_template("manage_caretakers.html", caretakers=caretakers, msg=msg, err=err)

from werkzeug.security import check_password_hash

@app.route('/caretaker/login', methods=['GET', 'POST'])
def caretaker_login():
    if request.method == 'POST':
        email = request.form['email'].strip()
        pw = request.form['password']
        caretaker = mongo.db.caretakers.find_one({'email': email, 'status': 'active'})
        if caretaker and check_password_hash(caretaker['password'], pw):
            session.clear()
            session['caretaker_id'] = str(caretaker['_id'])
            session['role'] = 'caretaker'
            return redirect(url_for('caretaker_dashboard'))
        else:
            return render_template('caretaker_login.html', error="Invalid email or password, or caretaker not active.")
    return render_template('caretaker_login.html')



@app.route('/caretaker/dashboard', methods=['GET', 'POST'])
def caretaker_dashboard():
    if 'caretaker_id' not in session or session.get("role") != "caretaker":
        return redirect(url_for('caretaker_login'))
    caretaker = mongo.db.caretakers.find_one({'_id': ObjectId(session['caretaker_id'])})
    if not caretaker:
        session.clear()
        return redirect(url_for('caretaker_login'))
    msg = None
    if request.method == 'POST':
        slot_id = request.form['slot_id']
        new_status = request.form['new_status']
        mongo.db.slots.update_one({'slot_id': slot_id}, {'$set': {'status': new_status}})
        msg = f"Slot {slot_id} marked as {new_status}."

        # Log this change as an admin notification
        mongo.db.notifications.insert_one({
            "type": "maintenance",
            "slot_id": slot_id,
            "user_id": None,
            "message": f"Caretaker {caretaker['name']} set slot {slot_id} status to {new_status}.",
            "created_at": datetime.utcnow(),
            "read": False
        })

    assigned_slots = list(mongo.db.slots.find({'slot_id': {'$in': caretaker['assigned_slots']}}))
    return render_template("caretaker_dashboard.html", caretaker=caretaker, slots=assigned_slots, msg=msg)

@app.route('/admin/new_maintenance_notifications')
def new_maintenance_notifications():
    n = mongo.db.notifications.find_one({"type":"maintenance","read":False})
    if n:
        return jsonify({'message':n['message']})
    return jsonify({'message':None})


def send_reminder_emails():
    now = datetime.utcnow()
    soon = now + timedelta(minutes=15)
    # Find active bookings ending in about 15 minutes
    bookings = mongo.db.bookings.find({
        "status": "active",
        "end_time": {
            "$gte": now.strftime("%Y-%m-%dT%H:%M"),
            "$lte": soon.strftime("%Y-%m-%dT%H:%M")
        },
        "reminder_sent": {"$ne": True}
    })
    for booking in bookings:
        user = mongo.db.users.find_one({'user_id': booking['user_id']})
        if user and user.get('email'):
            # Send the reminder
            try:
                msg = Message(
                    subject="PARKiT: Booking Ending Soon",
                    recipients=[user['email']],
                    body=f"""Hi,

This is your friendly reminder that your parking slot (#{booking['slot_id']}) will end at {booking['end_time']}.

Please vacate the slot or renew your booking if needed.

Thank you for using PARKiT!
                    """
                )
                mail.send(msg)
                # Mark as reminded to avoid sending multiple times
                mongo.db.bookings.update_one(
                    {'_id': booking['_id']}, {'$set': {'reminder_sent': True}}
                )
            except Exception as e:
                print(f"Failed to send reminder to {user['email']}: {e}")

# Set up the background scheduler to run every 2 minutes
scheduler = BackgroundScheduler()
scheduler.add_job(func=send_reminder_emails, trigger="interval", minutes=2)
scheduler.start()


@app.route("/")
@app.route("/home")
def home():
    return render_template("base.html")

if __name__ == "__main__":
    app.run(debug=True)

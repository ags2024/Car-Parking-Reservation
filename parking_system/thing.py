from flask import Flask
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash

app = Flask(__name__)
app.config["MONGO_URI"] = "mongodb+srv://reddycherish76:Cherish1302@dataleak.zr189.mongodb.net/parking_system"
mongo = PyMongo(app)

with app.app_context():
    mongo.db.admins.insert_one({
        'username': 'admin',
        'password': generate_password_hash('admin')   # Replace with your password
    })
    print("Admin user created.")

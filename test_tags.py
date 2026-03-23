from database import Database
import json

db = Database()
# Force some tags into local DB for testing
db.add_title_tag('mario kart', 'hot')
db.add_title_tag('animal crossing', 'hot')
db.add_title_tag('solo dlc', 'dlc')

# print tests
games = [
    {"name": "Mario Kart 8 Deluxe | Solo DLC"},
    {"name": "Animal Crossing: New Horizons"},
]
print("Before:", games)
res = db.apply_title_tags(games)
print("After:", res)

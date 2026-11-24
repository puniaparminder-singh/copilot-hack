# Create a base Flask server

import pickle
from pathlib import Path

from flask import Flask, request, jsonify

app = Flask(__name__)

# Enable cors
@app.after_request
def after_request(response):
    """
    Enable CORS
    """
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    return response


# Load model from pickle file
BASE_DIR = Path(__file__).resolve().parent
with (BASE_DIR / 'model.pkl').open('rb') as model_file:
    model = pickle.load(model_file)

# Model takes two parameters - day of week and airport id, then returns a prediction of flight delay
@app.route('/predict', methods=['GET'])
def predict():
    """
    Takes two parameters - day of week and airport id, then returns a prediction of flight delay
    """
    # Store day_of_week and airport_id as ints
    day_value = request.args.get('day_of_week')
    airport_value = request.args.get('airport_id')
    if day_value is None or airport_value is None:
        return jsonify({'error': 'day_of_week and airport_id are required'}), 400

    try:
        day_of_week = int(day_value)
        airport_id = int(airport_value)
    except (TypeError, ValueError):
        return jsonify({'error': 'day_of_week and airport_id must be integers'}), 400

    prediction = model.predict_proba([[day_of_week, airport_id]])[0]

    certainty = float(prediction[0])
    delay = float(prediction[1])

    # return prediction as json
    return jsonify({'certainty': certainty, 'delay': delay})

# Create a new route called airports with method of get
@app.route('/airports', methods=['GET'])
def airports():
    # Load airports from csv file
    with (BASE_DIR / 'airports.csv').open('r', encoding='utf-8') as airport_file:
        airports = airport_file.readlines()

    # Remove first line of airports
    airports.pop(0)

    # Create list with dictionary of airports
    # First value is id, second is name
    # Convert id to integer
    # Remove last character from name
    airports = [{'id': int(airport.split(',')[0]), 'name': airport.split(',')[1][:-1]} for airport in airports]
    # Sort by name
    airports = sorted(airports, key=lambda k: k['name'])

    return jsonify(airports)

if __name__ == '__main__':
    app.run(debug=True)
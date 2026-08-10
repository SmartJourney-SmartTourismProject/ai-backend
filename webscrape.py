from bs4 import BeautifulSoup
import requests

url = 'https://www.booking.com/'  # Replace with the URL you want to scrape
respons = requests.get(url)

print(respons.status_code)  # Print the status code to check if the request was successful
print(respons.text)  # Print the HTML content of the page
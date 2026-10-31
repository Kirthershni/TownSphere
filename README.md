# TownSphere – Smart Residential Operations Platform

Team Name: DYNOVA  
Institution: VIT Chennai  
Project Type: Smart Residential Operations Platform

## About the Project

TownSphere is a web-based residential operations platform designed to simplify complaint management and maintenance activities within residential communities.

The platform connects residents, maintenance staff, and administrators through a centralized system. Residents can submit complaints and track their progress, maintenance staff can manage assigned tasks, and administrators can oversee complaints, staff, and assignments.

## Key Features

- Resident Module
  - Submit maintenance complaints with optional images.
  - Track complaint status.
  - Validate complaint completion.
  - Submit ratings and feedback.

- Maintenance Staff Module
  - View assigned complaints.
  - Accept or reject assigned tasks.
  - Update work progress.
  - Upload progress images.

- Administrator Module
  - Manage residents and maintenance staff.
  - View, assign, and reassign complaints.
  - Monitor complaint progress.

- Worker Allocation
  - Considers staff specialization, skills, availability, workload, and distance.
  - Uses a weighted scoring mechanism to select suitable staff.

## Technology Stack

Frontend : HTML, CSS, JavaScript 
Backend : Python Flask 
Database : Oracle Database 
Database Connectivity : python-oracledb 
Development Environment : Visual Studio Code 
Version Control : Git and GitHub 


## Repository Structure

TownSphere/
  - database/
    schema.sql
    sample_data.sql
    README.md
  - outputs/
  - static/
  - templates/
  - app.py
  - db.py
  - dispatcher.py
  - requirements.txt
  - .gitignore
  - README.md

## Database Setup

The `database` folder contains the SQL files required to prepare the database.

1. Open Oracle Database and connect using your database credentials.
2. Execute `database/schema.sql` to create the required tables.
3. Execute `database/sample_data.sql` to insert fictional sample records.

Note: The sample data is intended for demonstration purposes. Replace placeholder password hashes before using any sample accounts for authentication.

## Running the Project Locally

1. Clone the repository:

   git clone https://github.com/Kirthershni/TownSphere.git
   
2. Navigate to the project directory:
   
   cd TownSphere

4. Install the required dependencies:

   pip install -r requirements.txt

5. Configure the database connection using environment variables:

   DB_USER
   DB_PASSWORD
   DB_DSN

6. Run the Flask application:

   python app.py

7. Open the local URL displayed in the terminal.


## GitHub Repository

https://github.com/Kirthershni/TownSphere

## Future Enhancements

- Community Hub for announcements, events, polls, and offers.
- Maintenance analytics and performance reports.
- Dedicated Android application with push notifications.



**Developed by Team DYNOVA | VIT Chennai**

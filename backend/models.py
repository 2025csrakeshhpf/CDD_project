from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    usn = Column(String, default="4IK25CS161")
    college = Column(String, default="Visvesvaraya Technological University (VTU)")
    branch = Column(String, default="Computer Science Engineering")
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="student")
    preferred_language = Column(String, default="python")
    daily_goal_problems = Column(Integer, default=5)
    reset_token = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProgrammingLanguage(Base):
    __tablename__ = "programming_languages"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=False)
    is_supported = Column(Boolean, default=True)
    runtime_info = Column(String, default="")


class CodingProblem(Base):
    __tablename__ = "coding_problems"

    id = Column(Integer, primary_key=True, index=True)
    problem_number = Column(Integer, unique=True, index=True, nullable=False)
    title = Column(String, index=True, nullable=False)
    topic = Column(String, index=True, nullable=False)
    difficulty = Column(String, index=True, nullable=False)
    company = Column(String, index=True, default="Google")
    description = Column(Text, nullable=False)
    input_format = Column(Text, nullable=False)
    output_format = Column(Text, nullable=False)
    constraints = Column(Text, nullable=False)
    concept_tag = Column(String, default="")
    complexity_note = Column(String, default="Time: O(n), Space: O(n)")
    hints_json = Column(Text, default="[]")
    starters_json = Column(Text, default="{}")
    reference_solution = Column(Text, default="")


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, index=True)
    problem_id = Column(Integer, ForeignKey("coding_problems.id"), index=True, nullable=False)
    input_data = Column(Text, nullable=False)
    expected_output = Column(Text, nullable=False)
    is_sample = Column(Boolean, default=True)


class AptitudeQuestion(Base):
    __tablename__ = "aptitude_questions"

    id = Column(Integer, primary_key=True, index=True)
    question_number = Column(Integer, unique=True, index=True, nullable=False)
    topic = Column(String, index=True, nullable=False)
    difficulty = Column(String, index=True, nullable=False)
    company = Column(String, default="TCS")
    question_text = Column(Text, nullable=False)
    options_json = Column(Text, nullable=False)
    correct_option = Column(String, nullable=False)
    solution_text = Column(Text, nullable=False)


class StudentSubmission(Base):
    __tablename__ = "student_submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    problem_id = Column(Integer, ForeignKey("coding_problems.id"), index=True, nullable=False)
    language = Column(String, nullable=False)
    mode = Column(String, default="submit")
    source_code = Column(Text, nullable=False)
    verdict = Column(String, nullable=False)
    passed = Column(Boolean, default=False)
    passed_cases = Column(Integer, default=0)
    total_cases = Column(Integer, default=0)
    execution_time_ms = Column(Float, default=0.0)
    time_taken_sec = Column(Integer, default=0)
    error_message = Column(Text, default="")
    ai_diagnosis = Column(Text, default="")
    submitted_at = Column(String, nullable=False)


class StudentAchievement(Base):
    __tablename__ = "student_achievements"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    code = Column(String, index=True, nullable=False)
    unlocked_at = Column(String, nullable=False)


class DailyActivity(Base):
    __tablename__ = "daily_activities"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    activity_date = Column(String, index=True, nullable=False)  # YYYY-MM-DD
    problems_solved = Column(Integer, default=0)
    submissions_count = Column(Integer, default=0)
    practice_seconds = Column(Integer, default=0)


class StudentActivityLog(Base):
    __tablename__ = "student_activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    icon = Column(String, default="⚡")
    message = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class UserDraft(Base):
    __tablename__ = "user_drafts"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    problem_id = Column(Integer, index=True, nullable=False)
    language = Column(String, index=True, nullable=False)
    code = Column(Text, nullable=False)
    updated_at = Column(String, nullable=False)


class Suggestion(Base):
    __tablename__ = "suggestions"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)
    student_name = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    is_anonymous = Column(Boolean, default=False)
    status = Column(String, default="Pending")
    admin_reply = Column(Text, default="")
    created_at = Column(String, nullable=False)


def get_session_factory(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)

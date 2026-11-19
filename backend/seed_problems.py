import json
import math
import sqlite3
from sqlalchemy.orm import Session
from models import AptitudeQuestion, CodingProblem, ProgrammingLanguage
from sandbox_judge import detect_supported_languages

TOPICS_23 = [
    "Arrays",
    "Strings",
    "Hash Tables",
    "Mathematics",
    "Dynamic Programming",
    "Sorting",
    "Searching",
    "Binary Search",
    "Linked Lists",
    "Stack",
    "Queue",
    "Trees",
    "Graphs",
    "Recursion",
    "Backtracking",
    "Greedy Algorithms",
    "Two Pointers",
    "Sliding Window",
    "Bit Manipulation",
    "Heap",
    "Database and SQL",
    "Object-Oriented Programming",
    "Advanced Data Structures",
]

COMPANIES = ["Google", "Amazon", "Microsoft", "TCS", "Infosys", "Wipro", "Accenture"]
CODING_PER_TOPIC = 1005   # Strictly 1000+ problems for EVERY topic
APTITUDE_TOTAL = 1005     # Strictly 1000+ aptitude problems

STARTERS_15 = {
    "python": "import sys\n\ndef solve():\n    data = sys.stdin.read().split()\n    if not data:\n        return\n    # Write your own solution here and print the result\n    \n\nif __name__ == '__main__':\n    solve()\n",
    "c": "#include <stdio.h>\n#include <stdlib.h>\n\nint main() {\n    // Write your C solution here\n    \n    return 0;\n}\n",
    "cpp": "#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    ios::sync_with_stdio(false);\n    cin.tie(nullptr);\n    // Write your C++ solution here\n    \n    return 0;\n}\n",
    "java": "import java.io.*;\nimport java.util.*;\n\npublic class Main {\n    public static void main(String[] args) throws Exception {\n        Scanner sc = new Scanner(System.in);\n        // Write your Java solution here\n        \n    }\n}\n",
    "javascript": "const fs = require('fs');\nconst tokens = fs.readFileSync(0, 'utf8').trim().split(/\\s+/);\n// Write your JavaScript solution here\n",
    "typescript": "import * as fs from 'fs';\nconst tokens: string[] = fs.readFileSync(0, 'utf8').trim().split(/\\s+/);\n// Write your TypeScript solution here\n",
    "csharp": "using System;\n\npublic class Program {\n    public static void Main() {\n        // Write your C# solution here\n    }\n}\n",
    "php": "<?php\n$input = trim(stream_get_contents(STDIN));\n// Write your PHP solution here\n?>\n",
    "ruby": "tokens = STDIN.read.split\n# Write your Ruby solution here\n",
    "go": "package main\n\nimport \"fmt\"\n\nfunc main() {\n\t// Write your Go solution here\n\t_ = fmt.Sprint\n}\n",
    "kotlin": "import java.util.Scanner\n\nfun main() {\n    val sc = Scanner(System.`in`)\n    // Write your Kotlin solution here\n}\n",
    "swift": "import Foundation\n// Write your Swift solution here\n",
    "rust": "use std::io::{self, Read};\n\nfn main() {\n    let mut input = String::new();\n    io::stdin().read_to_string(&mut input).unwrap();\n    // Write your Rust solution here\n}\n",
    "r": "input <- readLines('stdin')\n# Write your R solution here\n",
    "sql": "-- Write your SQL SELECT query below\nSELECT * FROM employees;\n",
}


def seed_database(db: Session, db_path: str):
    if db.query(ProgrammingLanguage).count() == 0:
        for lang in detect_supported_languages():
            db.add(ProgrammingLanguage(
                slug=lang["slug"],
                display_name=lang["display_name"],
                is_supported=lang["is_supported"],
                runtime_info=lang["runtime_info"],
            ))
        db.commit()

    existing_coding = db.query(CodingProblem).count()
    existing_apt = db.query(AptitudeQuestion).count()
    target_coding = len(TOPICS_23) * CODING_PER_TOPIC

    if existing_coding >= target_coding and existing_apt >= APTITUDE_TOTAL:
        return

    # Fast bulk insert using SQLite executemany for 23,115 coding problems + 1,005 aptitude problems
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM test_cases")
    cur.execute("DELETE FROM coding_problems")
    cur.execute("DELETE FROM aptitude_questions")

    starters_str = json.dumps(STARTERS_15)
    diffs = ["Easy", "Medium", "Hard"]

    coding_rows = []
    case_rows = []
    prob_id = 1

    for t_idx, topic in enumerate(TOPICS_23):
        for i in range(1, CODING_PER_TOPIC + 1):
            diff = diffs[i % 3]
            comp = COMPANIES[(t_idx + i) % len(COMPANIES)]
            variant = i % 4

            if topic == "Database and SQL":
                threshold = 40000 + (i % 50) * 1000
                title = f"SQL Query #{i}: High-Salary Filter (>{threshold})"
                desc = f"Table `employees(id INT, name TEXT, dept TEXT, salary INT)` is preloaded. Write a SQL query to select `name` and `salary` where `salary > {threshold}` ordered by `salary` DESC."
                in_fmt = "Preloaded table: employees(id, name, dept, salary)"
                out_fmt = "Rows of: name salary"
                cons = "Standard SQL SELECT"
                concept = "SQL WHERE & ORDER BY"
                cx = "Time: O(n log n)"
                hints = json.dumps([f"Hint 1: Filter rows with WHERE salary > {threshold}.", "Hint 2: Sort using ORDER BY salary DESC."])
                sol = f"SELECT name, salary FROM employees WHERE salary > {threshold} ORDER BY salary DESC;"
                s1_in = f"CREATE TABLE employees(id INT, name TEXT, dept TEXT, salary INT); INSERT INTO employees VALUES (1,'Rakesh','CSE',{threshold+12000}),(2,'Ananya','ECE',{threshold-5000}),(3,'Kiran','CSE',{threshold+4000});"
                s1_out = f"Rakesh {threshold+12000}\nKiran {threshold+4000}"
                h1_in = f"CREATE TABLE employees(id INT, name TEXT, dept TEXT, salary INT); INSERT INTO employees VALUES (1,'Dev','IT',{threshold+25000});"
                h1_out = f"Dev {threshold+25000}"
                cases = [(prob_id, s1_in, s1_out, 1), (prob_id, h1_in, h1_out, 0)]

            elif variant == 0:
                k = (i % 4) + 2
                title = f"{topic} #{i}: Maximum {k}-Window Subarray Sum"
                desc = f"Given N integers, find and print the maximum sum of any contiguous subarray of length K = {k}."
                in_fmt = "First line: integer N.\nSecond line: N space-separated integers."
                out_fmt = f"Single integer: maximum contiguous sum of length {k}."
                cons = f"{k} <= N <= 10^5\n-10^4 <= arr[i] <= 10^4"
                concept = f"{topic} & Sliding Window"
                cx = "Time: O(n), Space: O(1)"
                hints = json.dumps([
                    f"Hint 1: Recomputing {k} elements each step takes O(N * {k}).",
                    f"Hint 2: Maintain a running sum of size {k}; add arr[r] and subtract arr[r - {k}]."
                ])
                sol = f"import sys\nd=sys.stdin.read().split()\nn=int(d[0])\na=list(map(int,d[1:1+n]))\nw=sum(a[:{k}])\nb=w\nfor r in range({k},n):\n w+=a[r]-a[r-{k}]\n b=max(b,w)\nprint(b)\n"
                a1 = [3, 1, 5, 2, 6, 4]
                a2 = [-4, -2, -1, -5, -3]
                a3 = [i % 9 + 2, i % 7 + 3, i % 5 + 1, i % 11 + 4, i % 6 + 2]
                cases = [
                    (prob_id, "6\n3 1 5 2 6 4", str(max(sum(a1[j:j+k]) for j in range(7-k))), 1),
                    (prob_id, "5\n-4 -2 -1 -5 -3", str(max(sum(a2[j:j+k]) for j in range(6-k))), 1),
                    (prob_id, f"5\n{' '.join(map(str, a3))}", str(max(sum(a3[j:j+k]) for j in range(6-k))), 0),
                ]

            elif variant == 1:
                title = f"{topic} #{i}: Distinct Element Counter (Set Variant {i})"
                desc = f"Given N integers (variant #{i}), compute and print the number of strictly unique integers."
                in_fmt = "First line: integer N.\nSecond line: N space-separated integers."
                out_fmt = "Single integer: count of distinct elements."
                cons = "1 <= N <= 10^5"
                concept = f"{topic} & Hash Set Deduplication"
                cx = "Time: O(n), Space: O(n)"
                hints = json.dumps(["Hint 1: Use a hash set to achieve O(1) average lookup.", "Hint 2: Print len(set(arr))."])
                sol = "import sys\nd=sys.stdin.read().split()\nn=int(d[0])\nprint(len(set(map(int,d[1:1+n]))))\n"
                cases = [
                    (prob_id, "6\n4 2 4 7 2 9", "4", 1),
                    (prob_id, "4\n8 8 8 8", "1", 1),
                    (prob_id, f"5\n{i} {i+1} {i} {i+2} {i+1}", "3", 0),
                ]

            elif variant == 2:
                title = f"{topic} #{i}: Euclidean GCD Pair (Batch {i})"
                desc = f"Given two positive integers A and B (problem #{i}), print their Greatest Common Divisor."
                in_fmt = "Single line: two space-separated integers A and B."
                out_fmt = "Single integer: gcd(A, B)."
                cons = "1 <= A, B <= 10^12"
                concept = f"{topic} & Number Theory"
                cx = "Time: O(log(min(A,B)))"
                hints = json.dumps(["Hint 1: Apply Euclid's algorithm: gcd(A, B) = gcd(B, A % B)."])
                sol = "import math,sys\na,b=map(int,sys.stdin.read().split())\nprint(math.gcd(a,b))\n"
                x, y = (i * 6) + 12, (i * 9) + 18
                cases = [
                    (prob_id, "48 18", "6", 1),
                    (prob_id, "17 31", "1", 1),
                    (prob_id, f"{x} {y}", str(math.gcd(x, y)), 0),
                ]

            else:
                shift = (i % 15) + 5
                title = f"{topic} #{i}: Sorted Lower-Bound Count (Threshold +{shift})"
                desc = f"Given a sorted non-decreasing array of N integers and target T, count how many elements are >= T."
                in_fmt = "First line: N and T.\nSecond line: N sorted integers."
                out_fmt = "Single integer: count of elements >= T."
                cons = "1 <= N <= 10^5"
                concept = f"{topic} & Binary Search"
                cx = "Time: O(log n)"
                hints = json.dumps(["Hint 1: Use binary search (lower_bound / bisect_left) to locate the first index >= T.", "Hint 2: Answer is N - index."])
                sol = "import bisect,sys\nd=sys.stdin.read().split()\nn,t=int(d[0]),int(d[1])\na=list(map(int,d[2:2+n]))\nprint(n-bisect.bisect_left(a,t))\n"
                cases = [
                    (prob_id, "5 10\n2 5 10 14 20", "3", 1),
                    (prob_id, "4 30\n1 3 5 7", "0", 1),
                    (prob_id, f"5 {shift+10}\n{shift} {shift+5} {shift+10} {shift+15} {shift+20}", "3", 0),
                ]

            coding_rows.append((
                prob_id, prob_id, title, topic, diff, comp, desc,
                in_fmt, out_fmt, cons, concept, cx, hints, starters_str, sol
            ))
            case_rows.extend(cases)
            prob_id += 1

    cur.executemany(
        """
        INSERT INTO coding_problems (
            id, problem_number, title, topic, difficulty, company, description,
            input_format, output_format, constraints, concept_tag, complexity_note,
            hints_json, starters_json, reference_solution
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        coding_rows,
    )
    cur.executemany(
        "INSERT INTO test_cases (problem_id, input_data, expected_output, is_sample) VALUES (?, ?, ?, ?)",
        case_rows,
    )

    # Seed 1,005 Aptitude Questions across Quantitative, Logical Reasoning, Probability, Percentage & Number Series
    apt_topics = ["Quantitative Aptitude", "Logical Reasoning", "Number Series", "Percentage & Profit", "Probability & Permutations"]
    apt_rows = []
    for q_idx in range(1, APTITUDE_TOTAL + 1):
        a_topic = apt_topics[q_idx % len(apt_topics)]
        diff = diffs[q_idx % 3]
        comp = COMPANIES[q_idx % len(COMPANIES)]
        q_type = q_idx % 5

        if q_type == 0:
            # Time & Work
            d1 = (q_idx % 8) + 6
            d2 = d1 * 2
            ans_val = round((d1 * d2) / (d1 + d2), 2)
            ans_str = f"{ans_val} days"
            opts = [ans_str, f"{round(ans_val + 1.5, 2)} days", f"{round(ans_val + 3.0, 2)} days", f"{max(1.0, round(ans_val - 1.2, 2))} days"]
            q_text = f"Q{q_idx}. A can finish a software module in {d1} days and B can finish it in {d2} days. Working together, in how many days will they complete the work?"
            sol_text = f"Solution: Combined 1-day work = 1/{d1} + 1/{d2} = ({d1}+{d2})/({d1}*{d2}). Total days = ({d1} * {d2}) / ({d1} + {d2}) = {ans_str}."
        elif q_type == 1:
            # Number Series: next = prev * 2 + step
            start = (q_idx % 9) + 2
            step = (q_idx % 4) + 1
            s1 = start
            s2 = s1 * 2 + step
            s3 = s2 * 2 + step
            s4 = s3 * 2 + step
            ans_str = str(s4 * 2 + step)
            opts = [str(s4 * 2), ans_str, str(s4 * 2 + step + 4), str(s4 * 2 - step)]
            q_text = f"Q{q_idx}. Find the missing next number in the series: {s1}, {s2}, {s3}, {s4}, ?"
            sol_text = f"Solution: Each term follows the pattern (previous × 2) + {step}. Therefore, {s4} × 2 + {step} = {ans_str}."
        elif q_type == 2:
            # Percentage Net Change
            inc = ((q_idx % 5) + 2) * 10  # 20%, 30%, 40%, 50%, 60%
            dec = 20
            net = round(inc - dec - (inc * dec) / 100.0, 1)
            ans_str = f"{net}%"
            opts = [f"{inc - dec}%", ans_str, f"{net + 4.0}%", f"{net - 3.5}%"]
            q_text = f"Q{q_idx}. A product's price is increased by {inc}% and then decreased by {dec}%. What is the net percentage change?"
            sol_text = f"Solution: Net % change = a + b + (ab/100) = +{inc} - {dec} - ({inc}×{dec}/100) = {ans_str}."
        elif q_type == 3:
            # Train Speed & Platform Length
            speed_kmh = 54
            speed_ms = 15
            train_len = 120 + (q_idx % 10) * 10
            time_sec = 20 + (q_idx % 8)
            plat_len = speed_ms * time_sec - train_len
            ans_str = f"{plat_len} m"
            opts = [f"{plat_len - 25} m", ans_str, f"{plat_len + 30} m", f"{plat_len + 50} m"]
            q_text = f"Q{q_idx}. A {train_len}m long train running at {speed_kmh} km/h crosses a platform in {time_sec} seconds. Find the length of the platform."
            sol_text = f"Solution: Speed = 54 × (5/18) = 15 m/s. Total distance = 15 × {time_sec} = {15*time_sec} m. Platform length = {15*time_sec} - {train_len} = {ans_str}."
        else:
            # Probability / Ratios
            red = (q_idx % 5) + 3
            blue = (q_idx % 4) + 4
            total = red + blue
            g = math.gcd(red, total)
            ans_str = f"{red // g}/{total // g}"
            opts = [ans_str, f"{blue // math.gcd(blue, total)}/{total // math.gcd(blue, total)}", f"1/{total}", f"{red}/{total + 2}"]
            q_text = f"Q{q_idx}. A bag contains {red} red balls and {blue} blue balls. One ball is drawn at random. What is the probability of drawing a red ball?"
            sol_text = f"Solution: Total balls = {red} + {blue} = {total}. P(Red) = {red}/{total} = {ans_str}."

        apt_rows.append((q_idx, q_idx, a_topic, diff, comp, q_text, json.dumps(opts), ans_str, sol_text))

    cur.executemany(
        """
        INSERT INTO aptitude_questions (
            id, question_number, topic, difficulty, company, question_text, options_json, correct_option, solution_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        apt_rows,
    )

    conn.commit()
    conn.close()

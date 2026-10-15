def build_recommendation(analytics: dict) -> dict:
    average = analytics["daily_average_kg"]
    maximum = analytics["maximum_kg"]
    peak_day = analytics["highest_waste_day"]
    latest = analytics["latest_waste_kg"]
    latest_day = analytics["latest_day"]

    if average > 0 and maximum >= average * 1.2:
        priority = "High"
        action = f"Increase collection frequency around {peak_day}."
        reason = f"The peak of {maximum:.1f} kg is at least 20% above the {average:.1f} kg daily average."
    elif latest <= average * 0.8:
        priority = "Low"
        action = "Consider reducing collection frequency, while continuing to monitor bin levels."
        reason = f"The latest recorded day, {latest_day} at {latest:.1f} kg, is at least 20% below the {average:.1f} kg daily average."
    else:
        priority = "Medium"
        action = "Keep the normal collection schedule and review the peak day."
        reason = f"The peak was {peak_day} at {maximum:.1f} kg and the latest day was {latest_day} at {latest:.1f} kg; neither rule indicates a schedule change."

    return {
        "type": "rule-based",
        "priority": priority,
        "action": action,
        "reason": reason,
        "high_waste_alert": f"Review collection coverage on {peak_day}, the highest day in this dataset.",
        "overflow_prevention": "Check fill levels before dispatch; bin readings in this demo are simulated.",
        "recycling_suggestion": "Track recycled weight separately before reporting a recycling percentage.",
    }
